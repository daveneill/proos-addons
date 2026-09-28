"""
ProOS Core -- the Savant feed (register 629, 28 Sep 2026).

A plain TCP line feed that a Savant component profile connects to. Savant's
Component Profile Developer's Guide says how a profile talks to a device: the
host CONNECTS OUT to the device's IP and port, sends commands framed by
send_postfix, and parses what comes back with status_messages framed by
receive_end_condition. ProOS Core is that device. Nothing runs on the Savant
host; the dealer places one profile and points it at the box.

Lines are UTF-8, LF-terminated, pipe-separated -- the shape the profile's
status_messages parse with `constant` / `data terminator="|"`:

    HELLO|proos|<core version>     first line on every connection
    ACT|<area_slug>|<verdict>      one per known room on connect (the
                                   snapshot), then one per CHANGE forever --
                                   the same verdict as
                                   sensor.proos_activity_<area_slug>
    DSP|<area_slug>|on|off         the room's display, from the same verdict
                                   sweep (rooms with a committed display only)
    PONG                           the answer to PING (the profile polls
                                   every 30 s with execute_on_schedule so
                                   Savant's own watchdog sees a live device)

And ONE line inbound, the Savant->ProOS direction (register 630): a Blueprint
state trigger calls the profile's ReportZone action, which sends

    SAV|<zone>|<key>|<value>       e.g. SAV|Living Room|service|SVC_AV_TV

Core hands it to the ActivityPublisher, which mirrors it as
sensor.proos_savant_<zone_slug> so ProOS's awareness includes what the Savant
remotes did. Savant never controls ProOS through this port: any other inbound
line is ignored and counted, never acted on.

The feed is a MIRROR of what the ActivityPublisher already computes -- one
truth, two outputs. It weighs no evidence of its own.

COMMISSIONED IN PRO, NOT THE PLATFORM (Dave, 28 Sep: "anything we build
needs to run through Pro integrations not native HA"): the on/off switch,
the status and the profile download live on Pro's Systems > Savant page,
kept in Core's own store (savant.json). No add-on option, no platform UI.
"""
from __future__ import annotations

import json
import os
import re
import select
import socket
import threading
import time

PORT = 25804
_BACKLOG = 5
_MAX_CLIENTS = 8
_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "savant.json")


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "x"


def load_config() -> dict:
    """Core's own record of the bridge: {"enabled": bool}. Written by Pro."""
    try:
        with open(_STORE) as fh:
            d = json.load(fh) or {}
        return {"enabled": bool(d.get("enabled", False))}
    except Exception:                                            # noqa: BLE001
        return {"enabled": False}


def save_config(enabled: bool) -> dict:
    d = {"enabled": bool(enabled)}
    try:
        os.makedirs(os.path.dirname(_STORE), exist_ok=True)
        tmp = _STORE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, _STORE)
    except Exception as e:                                       # noqa: BLE001
        print("  savantfeed · could not save config: %s" % e, flush=True)
    return d


class SavantFeed:
    def __init__(self, port: int = PORT, version: str = "", bind: str = "0.0.0.0"):
        self.port = int(port)
        self.version = str(version or "")
        self.bind = bind
        self._state: dict = {}          # (kind, area_slug) -> value (the snapshot)
        self._clients: list = []        # connected sockets
        self._bufs: dict = {}           # socket -> partial inbound line
        self._lock = threading.Lock()
        self._srv = None
        self._thread = None
        self._stop = False
        self.ignored = 0                # inbound lines that were not PING / SAV
        self.bound_port = None
        self.on_savant = None           # callback(zone, key, value) for SAV lines
        self.zones: dict = {}           # zone -> {key: value, "_at": ts} (what Savant told us)
        self.last_hello = None          # epoch of the last Savant connection
        self.last_ping = None           # epoch of the last PING (the profile's 30 s poll)
        self.started_at = None

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(_BACKLOG)
        srv.setblocking(False)
        self._srv = srv
        self.bound_port = srv.getsockname()[1]
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="proos-savantfeed")
        self._thread.start()
        print("  savantfeed · listening on tcp/%d for a Savant profile"
              % self.bound_port, flush=True)
        return self

    def stop(self):
        self._stop = True
        try:
            self._srv.close()
        except Exception:                                        # noqa: BLE001
            pass
        with self._lock:
            for c in list(self._clients):
                self._drop(c)

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._clients)

    def status(self) -> dict:
        """What Pro's Savant page shows: is the port open, is a profile on the
        line, when it last polled, every room's lines, every zone Savant told
        us about. Plain facts; nothing inferred."""
        now = time.time()
        with self._lock:
            rooms: dict = {}
            for (kind, slug), v in self._state.items():
                rooms.setdefault(slug, {})[kind.lower()] = v
            zones = {z: dict(d) for z, d in self.zones.items()}
            n = len(self._clients)
        return {"listening": not self._stop and self._srv is not None,
                "port": self.bound_port, "clients": n,
                "connected": n > 0,
                "last_hello": self.last_hello, "last_ping": self.last_ping,
                "ping_age": (now - self.last_ping) if self.last_ping else None,
                "rooms": rooms, "zones": zones, "ignored": self.ignored,
                "version": self.version}

    # -- what the ActivityPublisher tells us -------------------------------
    def publish(self, area_slug: str, verdict: str) -> bool:
        """A room's verdict. Broadcast only on CHANGE; the snapshot keeps
        the latest value for the next connection. Returns True if sent."""
        return self._set("ACT", area_slug, verdict)

    def publish_display(self, area_slug: str, display_state) -> bool:
        """The room's display, from the verdict sweep's own reading of it:
        on/playing/paused -> on, off/standby -> off, anything else (no
        display, no reading) -> not published — ProOS never invents."""
        st = str(display_state or "").lower()
        if st in ("on", "playing", "paused", "idle"):
            return self._set("DSP", area_slug, "on")
        if st in ("off", "standby"):
            return self._set("DSP", area_slug, "off")
        return False

    def _set(self, kind: str, area_slug: str, value) -> bool:
        slug = str(area_slug or "").strip()
        if not slug:
            return False
        v = str(value if value is not None else "")
        with self._lock:
            if self._state.get((kind, slug)) == v:
                return False
            self._state[(kind, slug)] = v
            line = self._line(kind, slug, v)
            for c in list(self._clients):
                self._send(c, line)
        return True

    def forget(self, area_slug: str) -> None:
        """A room that no longer exists leaves the snapshot; its last values
        are retracted as empty so Savant's variables are not a lie."""
        slug = str(area_slug or "").strip()
        with self._lock:
            gone = [k for k in self._state if k[1] == slug]
            for k in gone:
                del self._state[k]
                for c in list(self._clients):
                    self._send(c, self._line(k[0], slug, ""))

    def snapshot(self) -> dict:
        """ACT values by room (the verdict snapshot)."""
        with self._lock:
            return {slug: v for (kind, slug), v in self._state.items() if kind == "ACT"}

    # -- wire ----------------------------------------------------------------
    @staticmethod
    def _line(kind: str, slug: str, v: str) -> bytes:
        return ("%s|%s|%s\n" % (kind, slug, v)).encode("utf-8")

    def _hello(self) -> bytes:
        return ("HELLO|proos|%s\n" % self.version).encode("utf-8")

    def _send(self, c, data: bytes) -> None:
        try:
            c.sendall(data)
        except Exception:                                        # noqa: BLE001
            self._drop(c)

    def _drop(self, c) -> None:
        try:
            self._clients.remove(c)
        except ValueError:
            pass
        self._bufs.pop(c, None)
        try:
            c.close()
        except Exception:                                        # noqa: BLE001
            pass

    def _accept(self) -> None:
        try:
            c, _addr = self._srv.accept()
        except Exception:                                        # noqa: BLE001
            return
        c.setblocking(True)
        with self._lock:
            if len(self._clients) >= _MAX_CLIENTS:
                try:
                    c.close()
                except Exception:                                # noqa: BLE001
                    pass
                return
            self._clients.append(c)
            self._bufs[c] = b""
            # the snapshot: every known room, so a Savant that has just
            # (re)connected holds the same truth as one that never dropped
            self.last_hello = time.time()
            self._send(c, self._hello())
            for (kind, slug) in sorted(self._state, key=lambda k: (k[1], k[0])):
                self._send(c, self._line(kind, slug, self._state[(kind, slug)]))

    def _inbound(self, c) -> None:
        try:
            data = c.recv(4096)
        except Exception:                                        # noqa: BLE001
            data = b""
        if not data:
            with self._lock:
                self._drop(c)
            return
        with self._lock:
            buf = self._bufs.get(c, b"") + data
            todo = []
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip().rstrip("\r")
                word = text.upper()
                if word == b"PING".decode():
                    self.last_ping = time.time()
                    self._send(c, b"PONG\n")
                elif word.startswith("SAV|"):
                    todo.append(text)
                elif word:
                    self.ignored += 1           # one-way: nothing else is acted on
            self._bufs[c] = buf[-4096:]
        for text in todo:
            self._savant_line(text)

    def _savant_line(self, text: str) -> None:
        """SAV|<zone>|<key>|<value> — what a Blueprint trigger reported.
        Kept verbatim per zone and handed to the publisher's mirror."""
        parts = text.split("|", 3)
        if len(parts) != 4:
            self.ignored += 1
            return
        _, zone, key, value = [p.strip() for p in parts]
        if not zone or not key:
            self.ignored += 1
            return
        with self._lock:
            z = self.zones.setdefault(zone, {})
            z[key] = value
            z["_at"] = time.time()
        cb = self.on_savant
        if cb is not None:
            try:
                cb(zone, key, value)
            except Exception as e:                               # noqa: BLE001
                print("  savantfeed · zone report not mirrored: %s" % e, flush=True)

    def _loop(self) -> None:
        while not self._stop:
            with self._lock:
                socks = [self._srv] + list(self._clients)
            try:
                ready, _, _ = select.select(socks, [], [], 1.0)
            except Exception:                                    # noqa: BLE001
                continue
            for s in ready:
                if s is self._srv:
                    self._accept()
                else:
                    self._inbound(s)
