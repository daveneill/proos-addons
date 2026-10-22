"""ProOS Cameras — HomeKit Data Stream (HDS): the encrypted link the Home hub uses for recordings and snapshots.
Register 970u, Route 2.

Ported from HAP-NodeJS (Apache-2.0) src/lib/datastream/DataStreamServer.ts and DataStreamManagement.ts:
  - setup: the hub writes SetupDataStreamTransport (command, transport type, its 32-byte key salt); we answer with
    status, our TCP port and our own 32-byte salt. Keys = HKDF-SHA512(salt = hub salt + our salt, the HAP session's
    shared secret, "HDS-Read-Encryption-Key" / "HDS-Write-Encryption-Key", 32 bytes).
  - frames: type 1 (1 byte) + payload length (3 bytes, big-endian) + ChaCha20-Poly1305(payload, aad = those 4 bytes)
    + 16-byte tag; nonce = 4 zero bytes + 64-bit little-endian counter, one counter per direction.
  - payload: header length (1 byte) + header (HDS dict) + message (HDS dict).
  - the first frame tells us which prepared session the connection belongs to (the one whose key opens it); the
    first message must be control/hello; requests not answered in 10 s close the link.

Differences, stated: the listening socket is opened when the camera starts and stays open (HAP-NodeJS opens it on the
first setup and closes it when idle) — HAP-python answers writes synchronously, so the port must already exist.
HAP-python keeps no copy of the session's shared secret, so `install_key_keeper()` swaps in a HAPCrypto that keeps it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import hdscodec as C

log = logging.getLogger("proos_cameras.hds")

VERSION = "1.0"
READ_INFO = b"HDS-Read-Encryption-Key"      # accessory → controller
WRITE_INFO = b"HDS-Write-Encryption-Key"    # controller → accessory
MAX_PAYLOAD = 0xFFFFF
PREPARED_TIMEOUT = 10.0
HELLO_TIMEOUT = 10.0
RESPONSE_TIMEOUT = 10.0

# HDSStatus
SUCCESS, OUT_OF_MEMORY, TIMEOUT, HEADER_ERROR, PAYLOAD_ERROR, MISSING_PROTOCOL, PROTOCOL_SPECIFIC_ERROR = range(7)
# dataSend close / protocol-specific reasons
(R_NORMAL, R_NOT_ALLOWED, R_BUSY, R_CANCELLED, R_UNSUPPORTED, R_UNEXPECTED_FAILURE, R_TIMEOUT, R_BAD_DATA,
 R_PROTOCOL_ERROR, R_INVALID_CONFIGURATION) = range(10)
EVENT, REQUEST, RESPONSE = 1, 2, 3


# ---------- keeping the HAP session's shared secret ----------

def install_key_keeper():
    """HAP-python builds its session cipher from the pair-verify shared secret and then drops the secret. HDS keys come
    from that same secret, so keep it on the cipher object (one subclass; the library itself is not edited)."""
    from pyhap import hap_protocol
    from pyhap.hap_crypto import HAPCrypto
    if getattr(hap_protocol.HAPCrypto, "_proos_keeps_key", False):
        return

    class KeepingHAPCrypto(HAPCrypto):
        _proos_keeps_key = True

        def reset(self, shared_key):
            self.shared_key = bytes(shared_key)
            super().reset(shared_key)

    hap_protocol.HAPCrypto = KeepingHAPCrypto


def shared_key_for(driver, client_addr):
    conn = getattr(getattr(driver, "http_server", None), "connections", {}).get(client_addr)
    crypto = getattr(conn, "hap_crypto", None)
    return getattr(crypto, "shared_key", None)


def derive_keys(shared_secret: bytes, controller_salt: bytes, accessory_salt: bytes):
    salt = controller_salt + accessory_salt

    def k(info):
        return HKDF(algorithm=hashes.SHA512(), length=32, salt=salt, info=info).derive(shared_secret)
    return k(READ_INFO), k(WRITE_INFO)      # (accessory→controller, controller→accessory)


# ---------- HAP TLV (setup characteristic) ----------

def tlv(t, v: bytes) -> bytes:
    out = bytearray()
    if not v:
        return bytes([t, 0])
    for i in range(0, len(v), 255):
        part = v[i:i + 255]
        out += bytes([t, len(part)]) + part
    return bytes(out)


def tlv_read(data: bytes):
    out, i = {}, 0
    while i + 2 <= len(data):
        t, n = data[i], data[i + 1]
        v = data[i + 2:i + 2 + n]
        out[t] = out.get(t, b"") + v
        i += 2 + n
    return out


SUPPORTED_TRANSPORT_CONFIGURATION = tlv(1, tlv(1, bytes([0])))     # one transport: HomeKit Data Stream over TCP


# ---------- frames ----------

def _nonce(n: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack("<Q", n)


def encode_payload(header: dict, message: dict) -> bytes:
    h, m = C.encode_bytes(header), C.encode_bytes(message)
    if len(h) > 255:
        raise ValueError("HDS header too long")
    return bytes([len(h)]) + h + m


def seal_frame(key: bytes, counter: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Tried sending payload with length larger than the maximum allowed for data stream")
    head = bytes([1]) + struct.pack(">I", len(payload))[1:]
    return head + ChaCha20Poly1305(key).encrypt(_nonce(counter), payload, head)


def split_frames(buf: bytes):
    """-> (list of (header4, ciphertext+tag) for type-1 frames, leftover bytes, too_big)."""
    frames, i = [], 0
    while i + 4 <= len(buf):
        ptype = buf[i]
        plen = int.from_bytes(buf[i + 1:i + 4], "big")
        if plen > MAX_PAYLOAD:
            return frames, b"", True
        if i + 4 + plen + 16 > len(buf):
            break
        if ptype == 1:
            frames.append((buf[i:i + 4], buf[i + 4:i + 4 + plen + 16]))
        else:
            log.debug("HDS: unknown payload type %d skipped", ptype)
        i += 4 + plen + 16
    return frames, buf[i:], False


def open_frame(key: bytes, counter: int, frame):
    head, body = frame
    try:
        return ChaCha20Poly1305(key).decrypt(_nonce(counter), body, head)
    except InvalidTag:
        return None


def decode_payload(payload: bytes):
    hl = payload[0]
    header = C.decode_bytes(payload[1:1 + hl])
    message = C.decode_bytes(payload[1 + hl:])
    protocol = header.get("protocol")
    if "event" in header:
        return {"type": EVENT, "protocol": protocol, "topic": header["event"], "id": None, "status": None, "message": message}
    if "request" in header:
        return {"type": REQUEST, "protocol": protocol, "topic": header["request"], "id": header.get("id"), "status": None, "message": message}
    if "response" in header:
        return {"type": RESPONSE, "protocol": protocol, "topic": header["response"], "id": header.get("id"),
                "status": header.get("status"), "message": message}
    raise C.HDSError("unknown payload header format: %r" % (header,))


# ---------- connection ----------

UNIDENTIFIED, EXPECTING_HELLO, READY, CLOSING, CLOSED = range(5)


class Connection(asyncio.Protocol):
    def __init__(self, server: "Server"):
        self.server = server
        self.loop = server.loop
        self.transport = None
        self.remote = None
        self.state = UNIDENTIFIED
        self.a2c = self.c2a = None
        self.n_out = self.n_in = 0
        self.buf = b""
        self.hap_addr = None
        self.handlers = {"control": {"request": {"hello": self._hello}}}
        self.responses = {}
        self.on_close = []
        self.hello_timer = None

    # transport
    def connection_made(self, transport):
        self.transport = transport
        self.remote = transport.get_extra_info("peername")
        sock = transport.get_extra_info("socket")
        try:
            import socket as _s
            sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
            sock.setsockopt(_s.SOL_SOCKET, _s.SO_KEEPALIVE, 1)
        except Exception:
            pass
        self.hello_timer = self.loop.call_later(HELLO_TIMEOUT, self._hello_late)
        self.server.connections.append(self)
        log.info("HDS: connection from %s", self.remote)

    def _hello_late(self):
        log.info("HDS [%s]: hello did not arrive in time — closing", self.remote)
        self.close()

    def connection_lost(self, exc):
        self.state = CLOSED
        if self.hello_timer:
            self.hello_timer.cancel()
        for rid, (cb, timer) in list(self.responses.items()):
            timer.cancel()
        self.responses.clear()
        if self in self.server.connections:
            self.server.connections.remove(self)
        for cb in list(self.on_close):
            try:
                cb(self)
            except Exception:
                log.exception("HDS: close handler failed")
        log.info("HDS [%s]: connection closed", self.remote)

    def data_received(self, data):
        if self.state >= CLOSING:
            return
        frames, self.buf, too_big = split_frames(self.buf + data)
        if too_big:
            log.info("HDS [%s]: frame larger than allowed — closing", self.remote)
            self.close()
            return
        if not frames:
            return
        plain = []
        k = 0
        if self.state == UNIDENTIFIED:
            first = frames[0]
            k = 1
            sess = self.server.identify(self, first)
            if sess is None:
                log.info("HDS [%s]: could not identify connection — closing", self.remote)
                self.close()
                return
            plain.append(sess.pop("first_plain"))
            self.a2c, self.c2a, self.hap_addr = sess["a2c"], sess["c2a"], sess["hap_addr"]
            self.n_in = 1
            self.state = EXPECTING_HELLO
        for f in frames[k:]:
            p = open_frame(self.c2a, self.n_in, f)
            if p is None:
                log.info("HDS [%s]: frame decryption or authentication failed — closing", self.remote)
                self.close()
                return
            self.n_in += 1
            plain.append(p)
        msgs = []
        for p in plain:
            try:
                msgs.append(decode_payload(p))
            except Exception as e:
                log.info("HDS [%s]: could not decode payload: %s", self.remote, e)
        if self.state == EXPECTING_HELLO:
            m = msgs[0] if msgs else None
            if not m or m["protocol"] != "control" or m["type"] != REQUEST or m["topic"] != "hello":
                log.info("HDS [%s]: first message was not hello: %r", self.remote, m)
                self.close()
                return
        for m in msgs:
            self._dispatch(m)

    def _dispatch(self, m):
        if m["type"] == RESPONSE:
            cb_timer = self.responses.pop(m["id"], None)
            if not cb_timer:
                log.debug("HDS: response to a request we did not send: %r", m)
                return
            cb, timer = cb_timer
            timer.cancel()
            try:
                cb(None, m["status"], m["message"])
            except Exception:
                log.exception("HDS: response handler failed")
            return
        kind = "event" if m["type"] == EVENT else "request" if m["type"] == REQUEST else None
        if kind is None:
            return
        h = self.handlers.get(m["protocol"])
        if h is None:
            self.server.dispatch_global(self, kind, m)
            return
        fn = h.get(kind, {}).get(m["topic"])
        if fn is None:
            log.debug("HDS [%s]: no %s handler for %s/%s", self.remote, kind, m["protocol"], m["topic"])
            return
        try:
            fn(m["message"]) if kind == "event" else fn(m["id"], m["message"])
        except Exception:
            log.exception("HDS: handler for %s/%s failed", m["protocol"], m["topic"])

    def _hello(self, rid, message):
        if self.hello_timer:
            self.hello_timer.cancel()
            self.hello_timer = None
        self.state = READY
        log.info("HDS [%s]: hello — ready", self.remote)
        self.send_response("control", "hello", rid)

    # handlers
    def add_protocol_handler(self, protocol, handler: dict) -> bool:
        if protocol in self.handlers:
            return False
        self.handlers[protocol] = handler
        return True

    def remove_protocol_handler(self, protocol, handler):
        if self.handlers.get(protocol) is handler:
            del self.handlers[protocol]

    # sending
    def _send(self, header, message):
        if self.state >= CLOSING:
            raise ConnectionError("Cannot send message on closing/closed socket!")
        frame = seal_frame(self.a2c, self.n_out, encode_payload(header, message))
        self.n_out += 1
        self.transport.write(frame)

    def send_event(self, protocol, event, message=None):
        if self.state == READY:
            self._send({"protocol": protocol, "event": event}, message or {})

    def send_request(self, protocol, request, message, callback):
        while True:
            rid = random.randrange(0, 4294967295)
            if rid not in self.responses:
                break

        def late():
            self.close()
            cb_timer = self.responses.pop(rid, None)
            if cb_timer:
                cb_timer[0](TimeoutError("timeout"), None, {})
        self.responses[rid] = (callback, self.loop.call_later(RESPONSE_TIMEOUT, late))
        self._send({"protocol": protocol, "request": request, "id": C.Int64(rid)}, message or {})

    def send_response(self, protocol, response, rid, status=SUCCESS, message=None):
        self._send({"protocol": protocol, "response": response, "id": C.Int64(rid), "status": C.Int64(status)},
                   message or {})

    def close(self):
        if self.state >= CLOSING:
            return
        self.state = CLOSING
        if self.transport:
            self.transport.close()

    def is_closed(self):
        return self.state >= CLOSING


# ---------- server ----------

class Server:
    def __init__(self, loop, host="0.0.0.0"):
        self.loop, self.host = loop, host
        self.port = None
        self._srv = None
        self.prepared = []
        self.connections = []
        self.global_handlers = {}       # (protocol, kind, topic) -> [fn(conn, ...)]

    async def start(self, port=0):
        self._srv = await self.loop.create_server(lambda: Connection(self), self.host, port)
        self.port = self._srv.sockets[0].getsockname()[1]
        log.info("HDS: listening on port %d", self.port)
        return self.port

    def stop(self):
        for c in list(self.connections):
            c.close()
        if self._srv:
            self._srv.close()
            self._srv = None

    def on(self, protocol, kind, topic, fn):
        self.global_handlers.setdefault((protocol, kind, topic), []).append(fn)

    def off(self, protocol, kind, topic, fn):
        fns = self.global_handlers.get((protocol, kind, topic), [])
        if fn in fns:
            fns.remove(fn)

    def dispatch_global(self, conn, kind, m):
        fns = list(self.global_handlers.get((m["protocol"], kind, m["topic"]), []))
        if not fns:
            log.debug("HDS [%s]: no handler for %s %s/%s", conn.remote, kind, m["protocol"], m["topic"])
            return
        for fn in fns:      # every listener hears it, as HAP-NodeJS's emitter (recording and snapshots both hear "open")
            try:
                fn(conn, m["message"]) if kind == "event" else fn(conn, m["id"], m["message"])
            except Exception:
                log.exception("HDS: global handler for %s/%s failed", m["protocol"], m["topic"])

    def prepare(self, shared_secret: bytes, controller_salt: bytes, hap_addr=None, accessory_salt: bytes = None):
        accessory_salt = accessory_salt or os.urandom(32)
        a2c, c2a = derive_keys(shared_secret, controller_salt, accessory_salt)
        sess = {"a2c": a2c, "c2a": c2a, "salt": accessory_salt, "hap_addr": hap_addr}
        sess["timer"] = self.loop.call_later(PREPARED_TIMEOUT, self._expire, sess)
        self.prepared.append(sess)
        return sess

    def _expire(self, sess):
        if sess in self.prepared:
            self.prepared.remove(sess)
            log.info("HDS: prepared session timed out — no connection within %ds", PREPARED_TIMEOUT)

    def identify(self, conn, first_frame):
        for sess in self.prepared:
            p = open_frame(sess["c2a"], 0, first_frame)
            if p is not None:
                self.prepared.remove(sess)
                sess["timer"].cancel()
                out = dict(sess)
                out["first_plain"] = p
                return out
        return None

    def hap_connection_lost(self, hap_addr):
        """HAP session gone → its data stream goes too (as HAP-NodeJS)."""
        for c in list(self.connections):
            if c.hap_addr == hap_addr:
                log.info("HDS [%s]: HAP connection closed — closing data stream", c.remote)
                c.close()


class Management:
    """The DataStreamTransportManagement service's write handler (setup of a new data stream)."""

    def __init__(self, server: Server):
        self.server = server
        self.last_response = b""

    def setup_write(self, value: bytes, shared_secret, hap_addr=None):
        """-> response bytes (status, port, our salt). Raises ValueError for a request we must refuse."""
        o = tlv_read(value)
        command = o.get(1, b"\xff")[:1]
        transport = o.get(2, b"\xff")[:1]
        salt = o.get(3, b"")
        if command != b"\x00":
            raise ValueError("unknown session command %r" % command)
        if transport != b"\x00" or len(salt) != 32:
            raise ValueError("bad transport or controller salt")
        if not shared_secret:
            raise ValueError("no HAP session secret for this connection")
        sess = self.server.prepare(shared_secret, salt, hap_addr)
        resp = tlv(1, bytes([0])) + tlv(2, tlv(1, struct.pack("<H", self.server.port)))
        self.last_response = resp
        return resp + tlv(3, sess["salt"])
