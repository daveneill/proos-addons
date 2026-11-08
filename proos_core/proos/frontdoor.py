"""ProOS'S OWN FRONT DOOR — build 1 of the "only serve ProOS" plan (register 808; Dave 13 Oct 06:15 "I need it all changed to
only serve ProOS", 06:49 "go").

READ FIRST (register 805): the apps and pages never use the platform's own pages. They use its data paths only — /api/*
(REST, the websocket, camera streams, images, webhooks), /auth/login_flow and /auth/token (sign-in), /auth/external/*
(an integration's own web sign-in coming back), /auth/providers, and /local/*. So ProOS's door answers a person with
ProOS's pages and its own sign-in, and passes ONLY those paths through to the platform inside the box, byte for byte.
Everything else of the platform's — its sign-in page, its screens, its settings — is never served here.

Build 1 is this door on Core's own port; the platform's port is untouched. Build 3 (with Dave's OK on the day) moves the
platform inside the box and this door onto the box's main address.

The platform's address inside the box is read from the Supervisor (/core/info: its address and port), never typed.
The passthrough is a plain pipe: the request goes in as it came (minus proxy headers the platform would refuse), the
platform's answer comes back as it left — streams and the websocket included.
"""
from __future__ import annotations

import socket
import threading
import time

PLATFORM_PREFIXES = ("api/", "local/", "auth/external/")
PLATFORM_EXACT = ("api", "auth/token", "auth/login_flow", "auth/providers")
NOT_PASSED = ("api/hassio_ingress/",)        # the Supervisor's own door — never through ours
DROP_HEADERS = ("connection", "keep-alive", "proxy-connection", "proxy-authorization", "x-forwarded-for",
                "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip", "x-ingress-path")
IDLE_S = 600            # plumbing: a passed-through connection with nothing moving either way for this long is closed
CONNECT_S = 10          # plumbing: how long reaching the platform inside the box may take
ADDR_KEPT_S = 60        # plumbing: how long the Supervisor's answer about the platform's address is reused
MARKER = "<script>window.PROOS_FRONT_DOOR=1</script>"

_ADDR = {"at": 0.0, "addr": None}


def is_platform_path(path):
    """True for the platform paths ProOS's pages and app use — and only those."""
    p = str(path or "").split("?")[0].lstrip("/")
    if any(p.startswith(x) for x in NOT_PASSED):
        return False
    pl = p.rstrip("/")
    return pl in PLATFORM_EXACT or pl.startswith("auth/login_flow/") or any(p.startswith(x) for x in PLATFORM_PREFIXES)


ANOTHER_DOOR_HEADERS = ("X-Ingress-Path", "X-Forwarded-For", "X-Remote-User-Id")


def came_through_another_door(headers):
    """True when a proxy in front of Core sent this request (the Supervisor's ingress adds X-Forwarded-For and, for a
    signed-in session, X-Remote-User-Id — read in its 2026.09.3 source; older Supervisors added X-Ingress-Path).
    A person's browser on the home network sends none of these."""
    return any(headers.get(h) for h in ANOTHER_DOOR_HEADERS)


def platform_address(info_fn, now=None):
    """(host, port) of the platform inside the box, from the Supervisor's /core/info — or None, said by the caller."""
    now = now or time.time()
    if _ADDR["addr"] and now - _ADDR["at"] < ADDR_KEPT_S:
        return _ADDR["addr"]
    try:
        d = info_fn("/core/info") or {}
    except Exception:                                            # noqa: BLE001
        d = {}
    host, port = d.get("ip_address"), d.get("port")
    if not host or not port:
        return _ADDR["addr"]          # the last answer the Supervisor gave, if any; never a made-up one
    _ADDR.update(at=now, addr=(str(host), int(port)))
    return _ADDR["addr"]


# ── ProOS ONLY (register 1261, step 3 of 3 — Dave 8 Nov 21:08: "my app is discovered by my app no other app will see it
# at all"; "no stupid pages that have prompts"). While the platform's config folder holds the quiet file (the same one
# switch that stops the platform announcing itself, door build 6), a sign-in reaches the platform ONLY for ProOS's own:
# the iPhone apps (home and Pro), the Android app (1260), and ProOS's pages — which sign in as their own address, the
# box's or ProOS Connect's. Anyone else is answered "not found", with nothing in it — no page, no reason.
# READ: every ProOS sign-in sends a client id (pro.html / dashboard.html: their origin; the apps: their identity; Core's
# own sign-ins go to the platform directly, never through this door). Nothing of ProOS calls /auth/providers.
APP_CLIENTS = ("https://protechsystems.com.au/proos", "https://protechsystems.com.au/proos-android")
OWN_DOMAIN = "proos.com.au"
SIGN_IN_PATHS = ("auth/login_flow", "auth/token", "auth/authorize")
CLOSED_PATHS = ("auth/providers",)      # only other apps ask it (to draw their own sign-in); ProOS never does


def _host_of(url):
    s = str(url or "").strip()
    if "://" not in s:
        return ""
    return s.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].lower()


def client_allowed(cid, headers=None):
    """True for a ProOS sign-in: one of ProOS's apps, a page on ProOS's own domain, or a page served from the very
    address this request came to (the box's pages sign in as their own origin)."""
    c = str(cid or "").strip()
    if c.rstrip("/") in APP_CLIENTS:
        return True
    host = _host_of(c)
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name == OWN_DOMAIN or name.endswith("." + OWN_DOMAIN):
        return True
    headers = headers or {}
    seen = {str(headers.get(h) or "").strip().lower() for h in ("Host", "X-Forwarded-Host")} - {""}
    return host in seen


def sign_in_path(path):
    """'closed' (nobody), 'sign_in' (ProOS only), or '' (not a sign-in) — for a path the door passes."""
    p = str(path or "").split("?")[0].strip("/")
    if p in CLOSED_PATHS:
        return "closed"
    if p in SIGN_IN_PATHS or p.startswith("auth/login_flow/"):
        return "sign_in"
    return ""


def client_of(method, path, body, ctype):
    """The client id a sign-in request names — from its address (authorize), its JSON (login flow) or its form
    (token). None when it names none."""
    from urllib.parse import parse_qs, urlsplit
    import json as _json
    q = parse_qs(urlsplit(str(path or "")).query)
    if q.get("client_id"):
        return q["client_id"][0]
    raw = body or b""
    if "json" in str(ctype or "").lower():
        try:
            d = _json.loads(raw.decode("utf-8") or "{}")
            return d.get("client_id") if isinstance(d, dict) else None
        except Exception:                                        # noqa: BLE001
            return None
    try:
        f = parse_qs(raw.decode("utf-8"))
    except Exception:                                            # noqa: BLE001
        return None
    return (f.get("client_id") or [None])[0]


def is_revoke(body):
    """A sign-in being ENDED (the token address with action=revoke) names no client — it only ever takes access away."""
    from urllib.parse import parse_qs
    try:
        return (parse_qs((body or b"").decode("utf-8")).get("action") or [""])[0] == "revoke"
    except Exception:                                            # noqa: BLE001
        return False


def refuse(handler):
    """Plain not-found, nothing in it."""
    handler.close_connection = True
    handler.send_response(404)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    return "refused"


def only_proos_on(config_dir, quiet_file):
    import os
    return os.path.exists(os.path.join(config_dir, quiet_file))


def wants_upgrade(headers):
    return "websocket" in str(headers.get("Upgrade") or "").lower()


def request_head(command, path, headers):
    """The request as the platform should see it: same method, path and headers; proxy headers dropped; one request
    per connection unless it is a websocket upgrade."""
    up = wants_upgrade(headers)
    lines = ["%s %s HTTP/1.1" % (command, path)]
    for k, v in headers.items():
        if k.lower() in DROP_HEADERS:
            continue
        if k.lower() == "upgrade" and not up:
            continue
        lines.append("%s: %s" % (k, v))
    lines.append("Connection: Upgrade" if up else "Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def mark_page(html_bytes):
    """The page served through the door knows it: it then reaches Core AND the platform through its own address."""
    s = html_bytes.decode("utf-8")
    i = s.find("<head>")
    if i < 0:
        return html_bytes
    i += len("<head>")
    return (s[:i] + MARKER + s[i:]).encode("utf-8")


def _copy(src_read, dst_send, idle):
    """Copy until either side ends. src_read(n) -> bytes ('' at the end)."""
    while True:
        try:
            chunk = src_read(65536)
        except (socket.timeout, OSError, ValueError):
            return
        if not chunk:
            return
        try:
            dst_send(chunk)
        except OSError:
            return


def passthrough(handler, addr, connect=None, body=None):
    """Pass handler's request to the platform at addr and its answer back. Returns a word for the log. body: the
    request's body when the door already read it (to see who is signing in); otherwise it is streamed."""
    connect = connect or socket.create_connection
    if not addr:
        body = b'{"error": "the platform inside the box can\'t be found right now"}'
        handler.send_response(502)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return "no_address"
    try:
        up = connect(addr, CONNECT_S)
    except OSError as e:
        body = ('{"error": "the platform inside the box didn\'t answer (%s)"}' % type(e).__name__).encode()
        handler.send_response(502)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return "platform_silent"
    handler.close_connection = True
    upgrade = wants_upgrade(handler.headers)
    try:
        up.settimeout(IDLE_S)
        up.sendall(request_head(handler.command, handler.path, handler.headers))
        n = 0 if body is not None else int(handler.headers.get("Content-Length") or 0)
        if body:
            up.sendall(body)
        while n > 0:
            chunk = handler.rfile.read(min(n, 65536))
            if not chunk:
                break
            up.sendall(chunk)
            n -= len(chunk)
        client = handler.connection
        try:
            client.settimeout(IDLE_S)
        except OSError:
            pass
        if upgrade:
            reader = getattr(handler.rfile, "read1", handler.rfile.read)
            t = threading.Thread(target=_copy, args=(reader, up.sendall, IDLE_S), daemon=True)
            t.start()
            _copy(up.recv, client.sendall, IDLE_S)
            try:
                up.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            t.join(2)
        else:
            _copy(up.recv, client.sendall, IDLE_S)
        return "passed"
    finally:
        try:
            up.close()
        except OSError:
            pass
