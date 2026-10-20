"""ProOS Core — Music engine event tap (a Developer surface).

STAGE 3 REMAINDER, BUILD 4 (16 Aug 2026). The audit's last music item:
"MA's push stream goes in the bin." Building its consumer BLIND would
mean guessing the engine's event names and shapes — the exact inventing
the rescue forbids. So the readings come first: this tap holds ONE
listening connection to the engine and RECORDS the stream verbatim —
each frame stamped on receipt, counted by the engine's own event word —
behind the Developer-gated route GET /music/events/recent. The consumer
gets built from what this shows on the real box, never from
documentation memory.

The tap is deliberately dumb, and its honesty is the design:
- frames are kept RAW (the engine's own JSON text); an oversized frame
  is cut AND SAYS SO (truncated flag + the true size)
- the ring is bounded and the DROP COUNT is kept — a full ring never
  reads as "that's everything"
- a dead session is recorded, not hidden: connected=false, the error
  kept, the reconnect counted, backoff doubling to a ceiling
- "not listening" is never dressed as "quiet": a tap that never ran
  says running=false, and last_rx makes connected-but-silent visible

The listening socket blocks (no read timeout): a mid-frame timeout
would desync the frame reader, and the thread is a daemon the process
end reaps. TCP keepalive is set so a silently-dead peer is eventually
noticed by the kernel; until then the tap SHOWS the silence (last_rx)
rather than guessing about it.
"""
from __future__ import annotations

import collections
import json
import socket
import threading
import time

from .ma_ws import MaClient
from .ha_ws import _recv_text

_RING = 200          # frames kept; older ones drop, counted, never silently
_FRAME_CAP = 4000    # chars stored per frame; a longer frame is cut AND SAYS SO
_BACKOFF_START = 5.0
_BACKOFF_MAX = 60.0


class MaTap:
    def __init__(self, get_conn):
        self.get_conn = get_conn
        self._mk = lambda h, p, t: MaClient(h, p, t)
        self._recv = lambda c: _recv_text(c._reader)             # noqa: SLF001
        self._lock = threading.Lock()
        self._events = collections.deque(maxlen=_RING)
        self._by_type: dict = {}
        self._total = 0
        self._dropped = 0
        self._running = False
        self._connected = False
        self._since = None
        self._last_rx = None
        self._last_error = None
        self._reconnects = 0
        self._thread = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self):
        """Idempotent: the first Developer read starts the listener."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._since = time.time()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ma-tap")
            self._thread.start()

    def stop(self):
        with self._lock:
            self._running = False

    def _run(self):
        backoff = _BACKOFF_START
        while self._running:
            if self._listen_once():
                backoff = _BACKOFF_START     # a working session resets the ladder
            if not self._running:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    # ── one listening session ───────────────────────────────────────────────
    def _listen_once(self):
        """Connect, then record every frame until the session dies. Returns
        True when at least one frame was heard (a working session); the
        failure — including 'music not connected yet' — is recorded, never
        raised: the tap reports, the loop retries."""
        heard = False
        c = None
        try:
            conn = self.get_conn() if self.get_conn else None
            if not conn or not conn[0]:
                raise RuntimeError("music not connected yet")
            host, port, token = conn
            c = self._mk(host, port, token)
            c.connect()
            try:                                # a dead-silent peer: kernel's job
                c._sock.setsockopt(socket.SOL_SOCKET,                # noqa: SLF001
                                   socket.SO_KEEPALIVE, 1)
                c._sock.settimeout(None)                             # noqa: SLF001
            except Exception:                                        # noqa: BLE001
                pass
            with self._lock:
                self._connected = True
                self._last_error = None
            while self._running:
                raw = self._recv(c)
                heard = True
                self._ingest(raw)
        except Exception as e:                                       # noqa: BLE001
            with self._lock:
                self._last_error = str(e)[:300]
                self._reconnects += 1
        finally:
            with self._lock:
                self._connected = False
            if c is not None:
                try:
                    c.close()
                except Exception:                                    # noqa: BLE001
                    pass
        return heard

    def _ingest(self, raw: str):
        """Record one frame verbatim: stamped on receipt, counted by the
        engine's own event word, cut only at _FRAME_CAP — and the cut SAID."""
        now = time.time()
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = {}
        etype = str(msg.get("event") or "other")
        entry = {"ts": now, "event": etype, "size": len(raw),
                 "truncated": len(raw) > _FRAME_CAP,
                 "frame": raw[:_FRAME_CAP]}
        with self._lock:
            if len(self._events) == _RING:
                self._dropped += 1
            self._events.append(entry)
            self._total += 1
            self._by_type[etype] = self._by_type.get(etype, 0) + 1
            self._last_rx = now

    # ── the Developer's window ──────────────────────────────────────────────
    def snapshot(self, limit: int = 100) -> dict:
        with self._lock:
            evs = list(self._events)[-int(limit):]
            return {"running": self._running, "connected": self._connected,
                    "since": self._since, "last_rx": self._last_rx,
                    "last_error": self._last_error,
                    "reconnects": self._reconnects,
                    "total": self._total, "dropped": self._dropped,
                    "by_type": dict(self._by_type), "events": evs}
