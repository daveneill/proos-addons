"""
ProOS Core -- Music Assistant API client (stdlib only)
======================================================

A thin WebSocket client for the Music Assistant *server's own* API, so ProOS can
commission MA -- list/enable/disable providers and players, run a provider's
setup form (e.g. HEOS) -- without anyone opening MA's web UI. This is what makes
"all of it in ProCore" possible.

How auth works (the "ProOS only" bit)
-------------------------------------
The MA add-on advertises a Supervisor discovery record carrying its internal
{host, port, auth_token}. HA's own integration connects using exactly that; so
does ProOS Core. No long-lived token to mint in MA's UI, no browser login -- we
reuse the add-on's discovery token (read from the Supervisor by server.py).

Protocol
--------
  - connect ws://host:port/ws  -> server PUSHES a ServerInfoMessage (schema_version)
  - if schema >= 28: send {command:"auth", message_id, args:{token}} -> result
  - commands:        send {command, message_id, args:{...}}          -> result
    Results may arrive 'partial' in chunks -> accumulate until the final message.

Framing is the same raw RFC6455 used for HA (no permessage-deflate, so frames are
plain text); the low-level helpers are shared with ha_ws.
"""
from __future__ import annotations
import json
import os
import socket
import base64
import urllib.request
import urllib.error

from .ha_ws import _Reader, _send_text, _recv_text

# MA server API schema this client targets (auth required from 28).
AUTH_SCHEMA = 28


class MaAuthFailed(RuntimeError):
    """MA rejected the supplied credentials/token."""


def ma_login(host, port, username: str, password: str, timeout: float = 10.0) -> str:
    """POST /auth/login -> access_token. The only auth endpoint that needs no token.

    Raises MaAuthFailed on bad credentials (HTTP 401); lets connection errors
    (URLError) propagate so the caller can try the next host candidate.
    """
    url = f"http://{host}:{int(port)}/auth/login"
    data = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise MaAuthFailed("Invalid Home Assistant username or password") from None
        raise
    tok = d.get("access_token")
    if not tok:
        raise MaAuthFailed("Login did not return an access token")
    return tok


class MaClient:
    """Short-lived MA API connection: connect, auth, run command(s), close."""

    def __init__(self, host: str, port, token: str | None = None, timeout: float = 10.0,
                 ingress_user: tuple | None = None):
        self.host = host
        self.port = int(port)
        self.token = token
        self.timeout = timeout
        # When set to (user_id, username, display_name), connect via MA's HA-Ingress
        # channel: send X-Remote-User-* headers and SKIP the token `auth` command.
        # MA treats any connection on its internal ingress listener as pre-authed
        # and derives the role from these headers (admin for an HA admin user).
        self.ingress_user = ingress_user
        self._sock = None
        self._reader = None
        self._mid = 0
        self.server_info: dict | None = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def connect(self) -> dict:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        ingress_hdrs = ""
        if self.ingress_user:
            uid, uname, disp = self.ingress_user
            ingress_hdrs = (
                f"X-Remote-User-ID: {uid}\r\n"
                f"X-Remote-User-Name: {uname}\r\n"
                f"X-Remote-User-Display-Name: {disp or uname}\r\n"
            )
        handshake = (
            f"GET /ws HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"{ingress_hdrs}"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(handshake.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("MA WS handshake closed")
            resp += chunk
        head, _, leftover = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise ConnectionError(f"MA WS upgrade failed: {status}")
        self._reader = _Reader(self._sock, leftover)

        # First frame is the server info (pushed, no message_id).
        self.server_info = json.loads(_recv_text(self._reader))
        schema = self.server_info.get("schema_version", 0)
        if schema >= AUTH_SCHEMA and not self.ingress_user:
            if not self.token:
                raise ConnectionError(
                    f"MA requires auth (schema {schema}) but no token was provided")
            self.command("auth", token=self.token)  # raises on failure
        return self.server_info

    def command(self, command: str, **args):
        """Send one command; return its result (accumulating any partial chunks)."""
        self._mid += 1
        mid = str(self._mid)
        _send_text(self._sock, json.dumps(
            {"command": command, "message_id": mid, "args": args}))
        partial = None
        while True:
            msg = json.loads(_recv_text(self._reader))
            if msg.get("message_id") != mid:
                continue  # server info / events / other correlations
            if msg.get("error_code") is not None or msg.get("error") is not None:
                raise ConnectionError(
                    f"MA '{command}' error: {msg.get('error_code') or msg.get('error')} "
                    f"{msg.get('details', '')}".strip())
            result = msg.get("result")
            if msg.get("partial"):
                partial = partial or []
                partial.extend(result or [])
                continue
            if partial is not None:
                partial.extend(result or [])
                return partial
            return result

    def provider_auth(self, provider_domain: str, session_id: str, on_auth_url,
                      values: dict | None = None, timeout: float = 80.0,
                      action: str = "auth"):
        """Run a provider auth action and drive it to completion on ONE
        connection (MA ties the flow to a single socket). Most providers use the
        generic "auth" action; some (e.g. Apple Music) use their own, e.g.
        "CONF_ACTION_AUTH" — pass it via `action`. Confirmed MA behaviour:
          1. session_id must live INSIDE `values`.
          2. MA pushes an `auth_session` event ({event, object_id=session_id,
             data=<auth_url>}) the instant the flow starts — we hand that URL to
             `on_auth_url` so ProHost can open the popup.
          3. The get_entries reply does NOT return until the user completes login
             at MA's /callback/{session_id} (or MA times out), so we read with a
             long window and DO NOT retry (a retry re-registers the callback route
             and fails with 'already registered').
        Returns the filled config entries (token populated) on success."""
        vals = dict(values or {})
        vals["session_id"] = session_id
        self._mid += 1
        mid = str(self._mid)
        _send_text(self._sock, json.dumps({
            "command": "config/providers/get_entries", "message_id": mid,
            "args": {"provider_domain": provider_domain, "instance_id": None,
                     "action": action, "values": vals}}))
        import time as _t
        deadline = _t.time() + timeout
        self._sock.settimeout(3.0)
        fired = False
        while _t.time() < deadline:
            try:
                msg = json.loads(_recv_text(self._reader))
            except (TimeoutError, OSError):
                continue
            if (not fired and msg.get("event") == "auth_session"
                    and msg.get("object_id") == session_id):
                url = msg.get("data")
                if url:
                    fired = True
                    try:
                        on_auth_url(url)
                    except Exception:
                        pass
                continue
            if msg.get("message_id") != mid:
                continue
            if msg.get("error_code") is not None or msg.get("error") is not None:
                raise ConnectionError(
                    f"MA 'config/providers/get_entries' error: "
                    f"{msg.get('error_code') or msg.get('error')} "
                    f"{msg.get('details', '')}".strip())
            return msg.get("result")
        raise TimeoutError("Timed out waiting for authentication to complete")

    def auth_probe(self, provider_domain: str, session_id: str, seconds: float = 12.0):
        """Diagnostic: fire a provider 'auth' action (session_id inside values, as
        MA requires) and capture every frame for `seconds`, so we can learn the
        exact shape MA uses to deliver the auth URL (the AUTH_SESSION event) on a
        non-frontend connection. Secrets/URLs are masked — we report shape only."""
        import time as _t
        self._mid += 1
        mid = str(self._mid)
        _send_text(self._sock, json.dumps({
            "command": "config/providers/get_entries", "message_id": mid,
            "args": {"provider_domain": provider_domain, "instance_id": None,
                     "action": "auth", "values": {"session_id": session_id}}}))
        frames = []
        deadline = _t.time() + seconds
        self._sock.settimeout(2.0)
        while _t.time() < deadline:
            try:
                raw = _recv_text(self._reader)
            except (TimeoutError, OSError):
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                frames.append({"unparsed": raw[:60]}); continue
            shape = {"keys": sorted(msg.keys())}
            if "event" in msg:
                shape["event"] = msg.get("event")
                shape["object_id"] = msg.get("object_id")
            if msg.get("message_id") == mid:
                shape["is_reply"] = True
                shape["error"] = msg.get("error_code") or msg.get("error")
            # mask any value that looks like a URL or long token
            def _mask(v):
                s = str(v)
                if s.startswith("http"):
                    try:
                        from urllib.parse import urlparse
                        return "URL:" + urlparse(s).netloc
                    except Exception:
                        return "URL"
                return ("LEN%d" % len(s)) if len(s) > 24 else s
            d = msg.get("data")
            if d is not None:
                shape["data_masked"] = _mask(d) if not isinstance(d, (dict, list)) else type(d).__name__
            frames.append(shape)
            if shape.get("is_reply"):
                break
        return {"session_id_used": session_id, "frame_count": len(frames), "frames": frames}

    def close(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
