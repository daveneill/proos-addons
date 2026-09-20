"""
ProOS Core — no outside system may hold Core (13 Sep 2026, register 437).

Dave reconfigured his test network. The platform came back and every
companion app found it; ProOS froze. The freeze was not Core restarting and
not the network: Core kept calling the UniFi controller at an address that
no longer existed, each call sitting on a 15-second timeout, the login and
the fetch serialised behind a lock — 41 s and 86 s per request — and the
Pro page's own health pings queued behind them until its overlay said
"ProOS is restarting". A product that exists to monitor and recover was
taken down by one integration it could not reach.

The law this module holds: a call to any outside system fails FAST, and a
failure is REMEMBERED. Short timeout on the wire; after one transport
failure the system is marked unreachable and every caller gets an instant,
honest answer — what is unreachable, why, and when it will be tried again —
for HOLD seconds, instead of each caller paying the timeout in turn. A
success clears it. Brand-agnostic: one Backoff per outside system, worn by
the UniFi Network and UniFi Protect transports today and by anything added
later. Nothing here knows what the system is; it only knows whether it
answered.
"""
from __future__ import annotations

import threading
import time

TIMEOUT = 4          # seconds on the wire — a LAN controller answers in tens of ms
HOLD = 60            # seconds to remember "unreachable" before trying again


class Unreachable(Exception):
    """The system did not answer recently; nobody waited for it this time."""

    def __init__(self, name: str, why: str, retry_in: float):
        self.name, self.why, self.retry_in = name, why, retry_in
        super().__init__("%s unreachable — %s (trying again in %ds)"
                         % (name, why, max(0, round(retry_in))))


class Backoff:
    def __init__(self, name: str, hold: float = HOLD):
        self.name = name
        self.hold = hold
        self._until = 0.0
        self._why = ""
        self._failed_at = 0.0
        self._lock = threading.Lock()

    def check(self, now: float | None = None) -> None:
        """Raise Unreachable instantly while the hold is on."""
        now = now or time.time()
        with self._lock:
            if now < self._until:
                raise Unreachable(self.name, self._why, self._until - now)

    def failed(self, why: str, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._until = now + self.hold
            self._why = str(why)[:160]
            self._failed_at = now

    def ok(self) -> None:
        with self._lock:
            self._until = 0.0
            self._why = ""

    def status(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            held = now < self._until
            return {"name": self.name, "reachable": not held,
                    "why": self._why if held else "",
                    "retry_in": round(self._until - now) if held else 0,
                    "failed_at": self._failed_at or None}
