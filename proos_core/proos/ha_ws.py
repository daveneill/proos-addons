"""
Minimal Home Assistant WebSocket client (stdlib only)
=====================================================

Just enough to run ONE request/response against HA's WebSocket API from inside a
ProOS add-on -- specifically `config_entries/flow/progress`, which lists
in-progress config flows. That listing is the *only* way to read flows: HA's REST
flow index is POST-only (GET returns 405), so an integration that HA discovered
can only be found over the socket.

Stays true to Core's no-pip-deps rule: a raw socket + hand-rolled RFC6455 framing.
We deliberately do NOT negotiate permessage-deflate, so every server frame is
uncompressed text we can read directly. The connection is short-lived: connect,
auth, one command, close.

Endpoints:
  Add-on (Supervisor proxy):  ws://supervisor/core/websocket   (SUPERVISOR_TOKEN)
  Standalone (Mac dev):       ws://<host>:<port>/api/websocket  (long-lived token)
"""
from __future__ import annotations
import json
import os
import socket
import struct
import base64
from urllib.parse import urlparse


def _ws_target(base_url: str):
    """(host, port, path) for the WS endpoint that matches this REST base_url."""
    if base_url.rstrip("/") == "http://supervisor/core":
        return ("supervisor", 80, "/core/websocket")
    u = urlparse(base_url)
    host = u.hostname or "supervisor"
    port = u.port or (443 if u.scheme == "https" else 8123)
    return (host, port, "/api/websocket")


class _Reader:
    """Buffered socket reader (seeded with any bytes left over from the upgrade)."""
    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.buf = initial

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WS closed mid-frame")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _send_text(sock, text: str):
    """Send one masked text frame (client->server frames must be masked)."""
    payload = text.encode()
    n = len(payload)
    header = bytearray([0x81])  # FIN + text opcode
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _recv_text(reader: _Reader) -> str:
    """Read one complete text message; transparently skip ping/pong frames."""
    while True:
        b1, b2 = reader.read(2)
        opcode = b1 & 0x0F
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", reader.read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", reader.read(8))[0]
        mask = reader.read(4) if (b2 & 0x80) else b""
        data = reader.read(ln) if ln else b""
        if mask:  # not expected from server; unmask defensively
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        if opcode == 0x8:            # close
            raise ConnectionError("WS closed by server")
        if opcode in (0x9, 0xA):     # ping / pong -> ignore
            continue
        return data.decode(errors="replace")


def ws_command(base_url: str, token: str, msg_type: str,
               timeout: float = 10.0, **fields):
    """Run ONE arbitrary HA WebSocket command and return its `result`.

    Same short-lived connect/auth/one-command/close pattern as flow_progress,
    generalised so Core can issue registry writes (e.g.
    `config/entity_registry/update`) that have no REST equivalent. Raises on
    auth failure, upgrade failure, or a non-success result.
    """
    host, port, path = _ws_target(base_url)
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n"
            "\r\n"
        )
        sock.sendall(handshake.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS handshake closed")
            resp += chunk
        head, _, leftover = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise ConnectionError(f"WS upgrade failed: {status}")
        reader = _Reader(sock, leftover)
        msg = json.loads(_recv_text(reader))
        if msg.get("type") == "auth_required":
            _send_text(sock, json.dumps({"type": "auth", "access_token": token}))
            msg = json.loads(_recv_text(reader))
        if msg.get("type") != "auth_ok":
            raise ConnectionError(f"WS auth failed: {msg.get('type')} {msg.get('message', '')}")
        cmd = {"id": 1, "type": msg_type}
        cmd.update(fields)
        _send_text(sock, json.dumps(cmd))
        while True:
            m = json.loads(_recv_text(reader))
            if m.get("id") == 1 and m.get("type") == "result":
                if not m.get("success", True):
                    raise ConnectionError(f"{msg_type} failed: {m.get('error')}")
                return m.get("result")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def flow_progress(base_url: str, token: str, timeout: float = 10.0) -> list:
    """Return HA's in-progress config flows (list of dicts), or raise on failure."""
    host, port, path = _ws_target(base_url)
    key = base64.b64encode(os.urandom(16)).decode()
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n"
            "\r\n"
        )
        sock.sendall(handshake.encode())

        # Read the HTTP upgrade response; keep any trailing bytes (first WS frame).
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS handshake closed")
            resp += chunk
        head, _, leftover = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise ConnectionError(f"WS upgrade failed: {status}")
        reader = _Reader(sock, leftover)

        # Auth handshake.
        msg = json.loads(_recv_text(reader))
        if msg.get("type") == "auth_required":
            _send_text(sock, json.dumps({"type": "auth", "access_token": token}))
            msg = json.loads(_recv_text(reader))
        if msg.get("type") != "auth_ok":
            raise ConnectionError(f"WS auth failed: {msg.get('type')} {msg.get('message', '')}")

        # One command: list in-progress flows.
        _send_text(sock, json.dumps({"id": 1, "type": "config_entries/flow/progress"}))
        while True:
            m = json.loads(_recv_text(reader))
            if m.get("id") == 1 and m.get("type") == "result":
                if not m.get("success", True):
                    raise ConnectionError(f"flow/progress failed: {m.get('error')}")
                return m.get("result") or []
    finally:
        try:
            sock.close()
        except Exception:
            pass
