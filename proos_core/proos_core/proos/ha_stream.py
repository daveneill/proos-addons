"""
The live event stream from Home Assistant — Core LISTENS instead of polling.
============================================================================

STAGE 1 OF THE APPROVED RESCUE (Dave, 16 Aug 2026). The finding that started
it: he pulled a switch and HA's own log showed every device unavailable
within NINE SECONDS — while ProOS, built on top of that exact event stream,
said "Checking…" with zero faults. Core's only source of state was a
full-house `GET /api/states` poll every 5 seconds; the websocket HA uses to
push every change in under a second was opened for one command at a time and
closed (ha_ws.py's own docstring: "connect, auth, one command, close").
In 74 project documents, HA's push stream was never once named as the
first-line source. This module is that missing first line.

WHAT IT IS. One persistent WebSocket to HA (same stdlib framing as ha_ws —
no pip deps): authenticate, `subscribe_events` for `state_changed`, seed a
full snapshot once with `get_states`, then hold a live in-memory state cache
that every consumer reads. A state change reaches this cache in the time it
takes HA to push a frame — observed sub-second on a LAN.

WHAT IT IS NOT. Not a rule, not an interpreter. The cache holds exactly what
HA said, verbatim (state, attributes, last_changed). Anything HA has not
reported is served as `unavailable` with nothing invented — the same honest
contract RestHAClient.snapshot always had.

FAIL-OPEN, NEVER FAIL-SILENT. If the stream is down or stale, `healthy()`
is False and RestHAClient falls straight back to the REST poll — today's
behaviour, unchanged. Liveness is EARNED, not assumed: the reader sends a
WS ping every PING_EVERY seconds of silence and counts ANY frame (pong,
event, result) as proof of life; a socket quiet past STALE_AFTER is treated
as dead even if the TCP connection still pretends to be up. Reconnect uses
capped backoff and re-runs the full auth + subscribe + snapshot sequence,
so a reconnected cache is never a stale one.

All timing constants here are PLUMBING (reconnect pacing, keepalive) — they
state nothing about any home.

Benched by ha_stream_bench.py against a fake HA server speaking real
RFC6455 frames: cache seeding, sub-second event delivery, honest absence,
reconnect-and-resubscribe, masked client frames, and the REST fallback —
red-first on mutations.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time

from .ha_ws import _Reader, _send_text, _ws_target

PING_EVERY = 20.0      # seconds of silence before we ask "are you there?"
STALE_AFTER = 60.0     # no frame for this long = the stream is dead to us
BACKOFF_START = 1.0    # first reconnect delay
BACKOFF_MAX = 30.0     # never hammer a down HA harder than this


def _send_ping(sock):
    """One masked, empty WS ping (client frames MUST be masked — HA silently
    drops unmasked frames; the symptom is a hang, not an error)."""
    mask = os.urandom(4)
    sock.sendall(bytes([0x89, 0x80]) + mask)


class HaStream(threading.Thread):
    """Persistent HA event listener maintaining a live state cache."""

    def __init__(self, base_url: str, token: str):
        super().__init__(daemon=True, name="ha-stream")
        self._base_url = base_url
        self._token = token
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}
        self._connected = False
        # SEEDED IS PART OF HEALTHY (16 Aug 2026, found live: Assist told
        # Dave his Sonos was "currently unavailable" while the box's own
        # history showed it paused for hours — he had updated Core minutes
        # before). _connected went True at subscribe time, BEFORE the
        # full-house seed landed, so healthy() vouched for an EMPTY cache
        # and snapshot() served "unavailable" for the entire house — at
        # exactly the moments (updates, reconnects) someone is testing.
        # Until this session's seed lands, callers fall back to REST: the
        # truth, a beat slower.
        self._seeded = False
        self._last_rx = 0.0
        self._stop = threading.Event()
        # Observability — served, never guessed.
        self.events_seen = 0
        self.reconnects = 0
        self.last_event_iso = None
        self.last_error = None
        # STAGE 1 BUILD 2: consumers may hook change delivery (the watcher's
        # poke_if_watched). Called with the entity_id AFTER the cache holds
        # the new state, so a woken consumer always reads fresh. A broken
        # hook must never kill the stream — guarded at the call site.
        self.on_change = None
        # ── THE REGISTRY EPOCH (19 Aug 2026, register 242) ─────────────────
        # MEASURED ON DAVE'S BOX, not guessed: the Supervisor log showed
        # THREE full websocket connections — connect, upgrade, auth, one
        # command, close — every two seconds, steadily, ~90 a minute, for
        # the AREA, DEVICE and ENTITY registries. That is ctlbridge's 2s
        # sweep reading three lists that change when an installer renames a
        # room. Stage 1 killed exactly this pattern for STATE and left the
        # registries on it.
        #
        # This counter is how a cache can be honest about them. It goes up
        # whenever the platform SAYS a registry changed, and on every new
        # connection (a fresh session is a fresh epoch — a reconnected
        # cache is never a stale one, the same rule _session_reset keeps
        # for state). A reader that cached at generation N may serve that
        # cache only while the generation is still N.
        #
        # AND IF WE CANNOT WATCH, WE SAY SO. An older platform that refuses
        # the registry subscriptions leaves registry_watched() False, and
        # then nothing caches — full reads, exactly today's behaviour.
        # A cache whose invalidation channel is dead must not serve.
        self._reg_gen = 0
        self._reg_watch = False

    # ── what consumers read ────────────────────────────────────────────────
    def registry_generation(self) -> int:
        """Bumps when the platform reports a registry change, or on a new
        session. Meaningless unless registry_watched() and healthy()."""
        return self._reg_gen

    def registry_watched(self) -> bool:
        """True only while this session actually holds the three registry
        subscriptions. False = we cannot know about a rename, so no caller
        may cache a registry."""
        return bool(self._reg_watch and self.healthy())

    def healthy(self) -> bool:
        """True only while the socket is up, recently alive, AND this
        session's full-house seed has landed. Stale, disconnected or
        unseeded -> False -> callers fall back to REST. Never optimistic:
        vouching for an empty cache served the whole house as
        "unavailable" (the window this closed, 16 Aug 2026)."""
        return bool(self._connected and self._seeded
                    and (time.time() - self._last_rx) < STALE_AFTER)

    def _session_reset(self):
        """A new session's cache is not trustworthy until ITS seed lands —
        a reconnect must not serve yesterday's cache as today's."""
        self._seeded = False

    def snapshot(self, entity_ids) -> dict:
        """Same contract as RestHAClient.snapshot — verbatim HA state from the
        live cache; anything unknown is served as unavailable, never invented."""
        wanted = set(entity_ids)
        out = {}
        with self._lock:
            for eid in wanted:
                rec = self._states.get(eid)
                if rec is not None:
                    out[eid] = {"state": rec.get("state", "unavailable"),
                                "attributes": rec.get("attributes", {}) or {},
                                "last_changed": rec.get("last_changed"),
                                "missing": False}
                else:
                    # Not in a SEEDED cache = not on this box (the seed is
                    # the whole house). Same law as the REST path: the state
                    # word is unchanged for internal callers, `missing` is
                    # the fact — an id the house never heard of is not a
                    # broken device.
                    out[eid] = {"state": "unavailable", "attributes": {},
                                "last_changed": None, "missing": True}
        return out

    def status(self) -> dict:
        """For /health and the Pro surfaces: the stream states its own
        condition rather than being believed."""
        return {"connected": self._connected,
                "seeded": self._seeded,
                "healthy": self.healthy(),
                "events": self.events_seen,
                "reconnects": self.reconnects,
                "last_event": self.last_event_iso,
                "last_error": self.last_error}

    def stop(self):
        self._stop.set()

    # ── the listener ───────────────────────────────────────────────────────
    def run(self):
        backoff = BACKOFF_START
        while not self._stop.is_set():
            try:
                self._run_once()
                backoff = BACKOFF_START          # a good session resets pacing
            except Exception as e:               # noqa: BLE001
                self.last_error = str(e)[:200]
            self._connected = False
            if self._stop.is_set():
                return
            self.reconnects += 1
            self._stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _run_once(self):
        self._session_reset()      # this session vouches only for its own seed
        host, port, path = _ws_target(self._base_url)
        key = base64.b64encode(os.urandom(16)).decode()
        sock = socket.create_connection((host, port), timeout=10.0)
        try:
            sock.settimeout(PING_EVERY)
            sock.sendall((
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Authorization: Bearer {self._token}\r\n"
                "\r\n").encode())
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

            msg = json.loads(self._recv(reader, sock))
            if msg.get("type") == "auth_required":
                _send_text(sock, json.dumps(
                    {"type": "auth", "access_token": self._token}))
                msg = json.loads(self._recv(reader, sock))
            if msg.get("type") != "auth_ok":
                raise ConnectionError(
                    f"WS auth failed: {msg.get('type')} {msg.get('message', '')}")

            # Subscribe FIRST, snapshot second — an event landing between the
            # two is applied on top of the snapshot, so nothing is lost in the
            # gap. (Snapshot-first would silently drop that window.)
            _send_text(sock, json.dumps(
                {"id": 1, "type": "subscribe_events",
                 "event_type": "state_changed"}))
            _send_text(sock, json.dumps({"id": 2, "type": "get_states"}))
            # THE REGISTRY EPOCH (register 242). Three more subscriptions on
            # the SAME connection — no extra sockets — so a rename is heard
            # instead of polled for. A new session is a new epoch: anything
            # cached against the old generation is dropped by its holder.
            self._reg_gen += 1
            self._reg_watch = True
            for _i, _et in ((3, "area_registry_updated"),
                            (4, "device_registry_updated"),
                            (5, "entity_registry_updated")):
                _send_text(sock, json.dumps(
                    {"id": _i, "type": "subscribe_events", "event_type": _et}))

            pending_events = []
            seeded = False
            self._connected = True
            self._last_rx = time.time()
            while not self._stop.is_set():
                raw = self._recv(reader, sock)
                m = json.loads(raw)
                mtype = m.get("type")
                if mtype == "result":
                    if m.get("id") == 1 and not m.get("success", True):
                        raise ConnectionError(
                            f"subscribe_events refused: {m.get('error')}")
                    # A REFUSED REGISTRY SUBSCRIPTION IS NOT FATAL — state is
                    # what this stream exists for, and an older platform that
                    # does not offer these events must still get a live
                    # stream. It simply means we cannot know about a rename,
                    # so nothing may cache a registry. Said, not swallowed.
                    if m.get("id") in (3, 4, 5) and not m.get("success", True):
                        self._reg_watch = False
                        print("  [ha_stream] registry events unavailable (%s)"
                              " — registries will be read live every time"
                              % (m.get("error") or "refused"), flush=True)
                    if m.get("id") == 2:
                        if not m.get("success", True):
                            raise ConnectionError(
                                f"get_states refused: {m.get('error')}")
                        self._seed(m.get("result") or [])
                        seeded = True
                        for ev in pending_events:
                            self._apply(ev)
                        pending_events = []
                elif mtype == "event":
                    _ev = m.get("event") or {}
                    # ROUTED BY THE EVENT'S OWN WORD, and ONLY when it says
                    # one of the three. Anything without an event_type is
                    # treated exactly as before — a state change — so this
                    # branch cannot alter what already worked.
                    if _ev.get("event_type") in ("area_registry_updated",
                                                 "device_registry_updated",
                                                 "entity_registry_updated"):
                        self._reg_gen += 1
                        continue
                    data = _ev.get("data") or {}
                    if seeded:
                        self._apply(data)
                    else:
                        pending_events.append(data)
        finally:
            self._connected = False
            try:
                sock.close()
            except Exception:                    # noqa: BLE001
                pass

    def _recv(self, reader: _Reader, sock) -> str:
        """One text message. Unlike ha_ws._recv_text this counts EVERY frame
        (pong included) as proof of life, and sends a ping after PING_EVERY
        seconds of silence rather than blocking forever on a dead socket."""
        while True:
            try:
                b1, b2 = reader.read(2)
            except socket.timeout:
                if (time.time() - self._last_rx) >= STALE_AFTER:
                    raise ConnectionError("stream silent past STALE_AFTER")
                _send_ping(sock)
                continue
            opcode = b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", reader.read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", reader.read(8))[0]
            mask = reader.read(4) if (b2 & 0x80) else b""
            data = reader.read(ln) if ln else b""
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            self._last_rx = time.time()
            if opcode == 0x8:
                raise ConnectionError("WS closed by server")
            if opcode == 0x9:                    # server ping -> pong (masked)
                pmask = os.urandom(4)
                pdata = bytes(c ^ pmask[i % 4] for i, c in enumerate(data))
                sock.sendall(bytes([0x8A, 0x80 | len(data)]) + pmask + pdata)
                continue
            if opcode == 0xA:                    # pong -> proof of life only
                continue
            return data.decode(errors="replace")

    # ── the cache: verbatim HA, nothing invented ───────────────────────────
    def _seed(self, states: list):
        with self._lock:
            self._states = {
                rec["entity_id"]: {"state": rec.get("state", "unavailable"),
                                   "attributes": rec.get("attributes", {}) or {},
                                   "last_changed": rec.get("last_changed")}
                for rec in states if rec.get("entity_id")}
        self._seeded = True        # the seed landing IS what healthy() vouches for

    def _apply(self, data: dict):
        eid = data.get("entity_id")
        if not eid:
            return
        new = data.get("new_state")
        with self._lock:
            if new is None:
                # Entity REMOVED. Absence is served as unavailable — honest,
                # and identical to what REST would say tomorrow.
                self._states.pop(eid, None)
            else:
                self._states[eid] = {
                    "state": new.get("state", "unavailable"),
                    "attributes": new.get("attributes", {}) or {},
                    "last_changed": new.get("last_changed")}
        self.events_seen += 1
        self.last_event_iso = (new or {}).get("last_changed")
        cb = self.on_change
        if cb is not None:
            try:
                cb(eid)
            except Exception:                                    # noqa: BLE001
                pass          # a consumer's bug never stops the stream
