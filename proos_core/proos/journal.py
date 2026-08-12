"""
Event journal — the structured record of everything ProOS observes and does,
per room, plus the live fan-out stream that keeps every open Pro client in
sync (laptop + iPad + phone concurrently).

Design rules (locked 29 Jul 2026):
- Append-only JSONL per room under /data/journal/. Nothing here is parsed
  back into behavior — the engine never reads the journal. Write-only from
  the engine's perspective; read-only from Pro's. This is what keeps the
  journal additive and the verdict ladder frozen.
- Every event is self-describing: ts, room, type, and a flat data dict of
  immutable ids + facts. No free text that a later parser must guess at.
- One in-memory fan-out bus: SSE subscribers get every event (all rooms)
  plus presence beats. Files are the archive; the bus is the live wire.
"""
import json
import os
import queue
import threading
import time

JOURNAL_DIR = "/data/journal"
MAX_BYTES = 2_000_000          # per-room cap; oldest half dropped on rotate
MEM_RING = 400                 # recent events kept in memory per room

_lock = threading.Lock()
_rings = {}                    # room -> list of event dicts (newest last)
_subs = []                     # live SSE subscriber queues
_presence = {}                 # device_id -> {name, kind, section, ts}
PRESENCE_TTL = 20              # seconds without a beat = gone


# ── emit ────────────────────────────────────────────────────────────────────
def emit(room, etype, data=None):
    """Append one structured event and fan it out live. Never raises —
    a journal failure must never touch engine behavior."""
    try:
        ev = {"ts": round(time.time(), 3), "room": str(room or "site"),
              "type": str(etype), "data": dict(data or {})}
        with _lock:
            ring = _rings.setdefault(ev["room"], [])
            ring.append(ev)
            del ring[:-MEM_RING]
        _write(ev)
        _fanout({"kind": "journal", "event": ev})
    except Exception:
        pass


def _path(room):
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(room))
    return os.path.join(JOURNAL_DIR, f"{safe}.jsonl")


def _write(ev):
    try:
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        p = _path(ev["room"])
        try:
            if os.path.getsize(p) > MAX_BYTES:
                with open(p, "rb") as f:
                    keep = f.read()[-MAX_BYTES // 2:]
                nl = keep.find(b"\n")
                with open(p, "wb") as f:
                    f.write(keep[nl + 1:] if nl >= 0 else keep)
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    except Exception:
        pass


# ── read ────────────────────────────────────────────────────────────────────
def read(room, limit=200, since=None):
    """Recent events for one room, newest first. Memory ring first (hot
    path); falls back to the file after a restart."""
    limit = max(1, min(int(limit or 200), 1000))
    with _lock:
        ring = list(_rings.get(str(room), []))
    if len(ring) < limit:
        ring = _read_file(room, limit)
    out = [e for e in ring if (since is None or e["ts"] > float(since))]
    return list(reversed(out[-limit:]))


def _read_file(room, limit):
    try:
        with open(_path(room), "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        evs = []
        for ln in lines:
            try:
                evs.append(json.loads(ln))
            except ValueError:
                continue
        with _lock:                      # warm the ring for next time
            if not _rings.get(str(room)):
                _rings[str(room)] = evs[-MEM_RING:]
        return evs
    except OSError:
        return []


def rooms():
    """Rooms that have any journal, memory or disk."""
    seen = set(_rings.keys())
    try:
        for fn in os.listdir(JOURNAL_DIR):
            if fn.endswith(".jsonl"):
                seen.add(fn[:-6])
    except OSError:
        pass
    return sorted(seen)


def merged(limit=300):
    """THE MASTER LOG (register 112). Every room's recent events — including
    'service', the assistant's audit trail — as ONE stream, newest first.

    Dave, 12 Aug: the per-room Evidence Timeline "makes sense to me…
    build a filtered master log that shows everything." This is the one
    mechanism behind that surface: the same journals the room pages read,
    merged and sorted. Filtering is presentation — it happens on the glass,
    never here, so every surface filters the same truth."""
    limit = max(1, min(int(limit or 300), 1000))
    evs = []
    for rm in rooms():
        evs.extend(read(rm, limit=min(limit, 300)))
    evs.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return evs[:limit]


# ── live bus (SSE fan-out + multi-device presence) ─────────────────────────
def subscribe():
    q = queue.Queue(maxsize=500)
    with _lock:
        _subs.append(q)
    return q


def unsubscribe(q):
    with _lock:
        try:
            _subs.remove(q)
        except ValueError:
            pass


def _fanout(msg):
    with _lock:
        dead = []
        for q in _subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)       # slow client: drop it, it will reconnect
        for q in dead:
            try:
                _subs.remove(q)
            except ValueError:
                pass


def broadcast(kind, payload):
    """Non-journal live message (incidents refresh, presence, state delta)."""
    _fanout({"kind": kind, **(payload or {})})


def presence_beat(device_id, name=None, kind=None, section=None):
    if not device_id:
        return presence_list()
    with _lock:
        _presence[str(device_id)] = {
            "id": str(device_id), "name": str(name or "device"),
            "kind": str(kind or "browser"), "section": str(section or ""),
            "ts": time.time()}
    plist = presence_list()
    _fanout({"kind": "presence", "devices": plist})
    return plist


def presence_list():
    now = time.time()
    with _lock:
        gone = [k for k, v in _presence.items() if now - v["ts"] > PRESENCE_TTL]
        for k in gone:
            _presence.pop(k, None)
        return sorted(_presence.values(), key=lambda v: v["ts"], reverse=True)
