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

from .ha_ws import _Reader, _send_text, _recv_text

# MA server API schema this client targets (auth required from 28).
AUTH_SCHEMA = 28


class MaClient:
    """Short-lived MA API connection: connect, auth, run command(s), close."""

    def __init__(self, host: str, port, token: str | None = None, timeout: float = 10.0):
        self.host = host
        self.port = int(port)
        self.token = token
        self.timeout = timeout
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
        handshake = (
            f"GET /ws HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
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
        if schema >= AUTH_SCHEMA:
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

    def close(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
