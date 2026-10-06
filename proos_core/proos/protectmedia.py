"""ProOS Core — camera events from the platform's own UniFi Protect media, kept ready.

REGISTER 696 (Dave, 6 Oct 2026: "this is a premium app"; "would prefer the
performance to be in the core and less device dependant"; "Go").

Register 692 moved camera events to THE PLATFORM'S OWN Protect media — the
media browser its own pages use, and its own signed thumbnail proxy. That stays:
nothing here signs in to the console, and every event and picture still comes
from the platform. What changed is WHO does the reading, measured on Dave's box
6 Oct 2026 before this was written:

  * the page asked the platform once per camera per day — 24 requests and
    3.1 s for the Live strip, 48 for the 3-day Events grid — on every open,
    re-reading days that were finished and can never change;
  * every thumbnail was the camera's full picture, 3200x1800 and 661 KB, for
    a 148-pixel tile, all fetched at once (up to 500 on the Events grid). The
    platform's own width/height request is passed on to the console and
    ignored: the same 661 KB comes back.

So Core reads, shrinks and remembers:

  * events(): a finished day is read once per camera and kept; only TODAY is
    read again, and not more often than TODAY_TTL. The page asks once.
  * thumb(): each event's picture is fetched from the platform once, shrunk
    with libjpeg-turbo's own DCT scaling (djpeg -scale 1/4 → cjpeg) — the
    cheapest resize there is, cheap on a Pi — and kept on disk. An event's
    picture never changes, so it is never fetched twice. If the tool is not
    installed, the platform's picture is served as it came (slower, never
    wrong).

The labels the platform gives an event — "10/06/26 15:02:22 10s Object
Detection - Vehicle" — are in the home's local time; they are read in the
time zone the platform itself is set to, never the container's.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# ── plumbing, declared (ledger class: plumbing) ─────────────────────────────
TODAY_TTL = 20.0          # s — how stale today's list may be before it is read again
WATCH_FOR = 600.0         # s — Core keeps today fresh this long after anyone last looked
ROOT_TTL = 300.0          # s — the camera list (cameras are added rarely)
DAY_CACHE_MAX = 600       # finished (camera, day) lists kept in memory
THUMB_KEEP = 3000         # shrunk pictures kept on disk, oldest dropped first
WORKERS = 6               # platform reads in flight at once
LONG_SIDE = 800           # px — a picture is shrunk only while its long side stays at least this
SHRINK_ABOVE = 1200       # px — pictures with a long side up to this are served untouched
# 696c (Dave, 7 Oct 2026: "the strip pics … seem fuzzy"): UniFi's own cameras already send
# 360x360 detection pictures; 1.0.838 quartered EVERY picture, so those became 90x90. Only a
# picture that is genuinely large (the HIKVISION's 3200x1800) is shrunk, and never below 800.

# An event's id is one of TWO shapes on Dave's box (read 7 Oct 2026, register 696b): older events
# (a third-party camera's motion, audio) carry 24 hex characters; every Object Detection, Ring
# and the newer cameras' Motion carry a UUID. 1.0.838/839 accepted only the first and dropped
# all 130 of yesterday's detections and rings. Both shapes, and nothing else, ever reach a path.
_EVENT_ID = re.compile(r"^(?:[0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_TITLE = re.compile(r"^(\d\d)/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d) ((?:\d+[hms]\s?)+)\s*(.*)$")
_KINDS = (("person", "Person"), ("face", "Face"), ("vehicle", "Vehicle"),
          ("animal", "Animal"), ("package", "Package"), ("license plate", "License Plate"))
_VEHICLE_WORDS = re.compile(r"\b(car|suv|truck|van|sedan|ute|bus|motorcycle|bike)\b")

_ws = None                # callable(msg_type, **fields) -> result
_get_bytes = None         # callable(path) -> (ctype, bytes)
_data_dir = os.environ.get("PROOS_DATA_DIR", "/data")
_lock = threading.Lock()
_root = {"at": 0.0, "nvr": "", "cams": []}
_days: dict = {}          # (cam, "Y:M:D") -> list of events (finished days only)
_today: dict = {}         # (cam, "Y:M:D") -> (read_at, list)
_tz = {"name": None, "zone": None}
_thumb_locks: dict = {}
_watch = {"last_ask": 0.0, "hours": 72, "thread": None}


def configure(ws_call, get_bytes, data_dir=None):
    """Hand Core's own platform connection in (server.py does this at boot)."""
    global _ws, _get_bytes, _data_dir
    _ws, _get_bytes = ws_call, get_bytes
    if data_dir:
        _data_dir = data_dir


def forget():
    """Drop every remembered list (the pictures on disk stay — they never change)."""
    with _lock:
        _root.update(at=0.0, nvr="", cams=[])
        _days.clear()
        _today.clear()


# ── reading the platform's labels ───────────────────────────────────────────
def _zone():
    if _tz["zone"] is None:
        name = None
        try:
            name = ((_ws("get_config") or {}).get("time_zone")) if _ws else None
        except Exception:  # noqa: BLE001
            name = None
        try:
            from zoneinfo import ZoneInfo
            _tz["zone"] = ZoneInfo(name) if name else _dt.timezone.utc
            _tz["name"] = name or "UTC"
        except Exception:  # noqa: BLE001
            _tz["zone"], _tz["name"] = _dt.timezone.utc, "UTC"
    return _tz["zone"]


def parse_title(title, zone=None):
    """(start_ms, duration_ms, rest) from the platform's label, or None."""
    m = _TITLE.match(str(title or ""))
    if not m:
        return None
    mo, d, y, hh, mi, ss = (int(m.group(i)) for i in range(1, 7))
    try:
        at = _dt.datetime(2000 + y, mo, d, hh, mi, ss, tzinfo=zone or _zone())
    except ValueError:
        return None
    dur = 0
    for n, u in re.findall(r"(\d+)([hms])", m.group(7)):
        dur += int(n) * (3600 if u == "h" else 60 if u == "m" else 1)
    return int(at.timestamp() * 1000), dur * 1000, m.group(8)


def kind_of(rest):
    """"Motion Event" · "Ring Event" · "Object Detection - Face, Person" → a kind."""
    r = str(rest or "").strip()
    low = r.lower()
    if low.startswith("ring"):
        return "Doorbell"
    if low.startswith("motion"):
        return "Motion"
    if low.startswith("audio"):
        return "Audio"
    det = (r.split(" - ", 1)[1] if " - " in r else "").lower()
    for k, v in _KINDS:
        if k in det:
            return v
    if _VEHICLE_WORDS.search(det):
        return "Vehicle"
    return det.split(",")[0].strip().title() if det else "Event"


# ── the platform's media, read and remembered ──────────────────────────────
def root(force=False):
    """(nvr, [ {id, name, eid} ]) — the platform's own Protect media root."""
    now = time.time()
    with _lock:
        if not force and _root["nvr"] and now - _root["at"] < ROOT_TTL:
            return _root["nvr"], list(_root["cams"])
    res = _ws("media_source/browse_media", media_content_id="media-source://unifiprotect") or {}
    nvr = (str(res.get("media_content_id") or "").split("unifiprotect/", 1)[1:] or [""])[0].split(":")[0]
    cams = []
    for c in res.get("children") or []:
        mcid = str((c or {}).get("media_content_id") or "")
        if ":browse:" not in mcid or mcid.endswith(":browse:all"):
            continue
        em = re.search(r"camera_proxy/([^?]+)", str(c.get("thumbnail") or ""))
        cams.append({"id": mcid.split(":browse:", 1)[1], "name": c.get("title") or "",
                     "eid": em.group(1) if em else ""})
    with _lock:
        _root.update(at=now, nvr=nvr, cams=cams)
    return nvr, list(cams)


def _day_ids(cutoff_ms, end_ms, zone):
    out = []
    d = _dt.datetime.fromtimestamp(cutoff_ms / 1000, zone).date()
    last = _dt.datetime.fromtimestamp(end_ms / 1000, zone).date()
    while d <= last and len(out) <= 31:
        out.append("%d:%d:%d" % (d.year, d.month, d.day))
        d += _dt.timedelta(days=1)
    return out, "%d:%d:%d" % (last.year, last.month, last.day)


def _read_day(nvr, cam, day, zone):
    res = _ws("media_source/browse_media",
              media_content_id="media-source://unifiprotect/%s:browse:%s:all:range:%s" % (nvr, cam["id"], day)) or {}
    out = []
    for k in res.get("children") or []:
        p = parse_title(k.get("title"), zone)
        eid = (str(k.get("media_content_id") or "").split(":event:", 1)[1:] or [""])[0]
        if not p or not _EVENT_ID.match(eid):
            continue
        start, dur, rest = p
        out.append({"source": "platform", "event_id": eid, "mcid": k.get("media_content_id"),
                    "when": _dt.datetime.fromtimestamp(start / 1000, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "start": start, "end": start + dur, "camId": cam["id"], "cam": cam["name"],
                    "eid": cam["eid"], "kind": kind_of(rest), "hasThumb": bool(k.get("thumbnail"))})
    return out


def _list_for(nvr, cam, day, today):
    key = (cam["id"], day)
    now = time.time()
    with _lock:
        if day != today and key in _days:
            return _days[key]
        if day == today and key in _today and now - _today[key][0] < TODAY_TTL:
            return _today[key][1]
    evs = _read_day(nvr, cam, day, _zone())
    with _lock:
        if day == today:
            _today[key] = (now, evs)
            for k in [k for k in _today if k[1] != today]:      # yesterday's "today": dropped, so it is
                _today.pop(k)                                   # read once more, complete, as a finished day
        else:
            _days[key] = evs
            while len(_days) > DAY_CACHE_MAX:
                _days.pop(next(iter(_days)))
    return evs


def events(hours=24, cap=60, now_ms=None, cams=None):
    """{nvr, events:[…newest first], read, kept} — the cameras' events over the last
    `hours`, from the platform's own media. Finished days come from memory.

    `cams`: the names of the cameras the caller shows (the dashboard's room-assigned
    cameras). They are chosen BEFORE the newest `cap` are kept — register 696a, found
    on Dave's box the evening 1.0.838 went in: a camera in no room logged 358 motion
    events in a day, filled the newest 500, and every person and vehicle on the Front
    Door fell off the end before the page could drop that camera."""
    hours = max(1, min(int(hours or 24), 24 * 31))
    cap = max(1, min(int(cap or 60), 5000))
    end = int(now_ms if now_ms is not None else time.time() * 1000)
    cutoff = end - hours * 3600 * 1000
    want = {str(n).strip().lower() for n in (cams or []) if str(n).strip()}
    nvr, cams = root()
    if want:
        cams = [c for c in cams if (c.get("name") or "").strip().lower() in want]
    if not nvr:
        return {"nvr": "", "events": [], "error": "UniFi Protect is not set up"}
    zone = _zone()
    days, today = _day_ids(cutoff, end, zone)
    jobs = [(c, d) for c in cams for d in days]
    with _lock:
        cold = sum(1 for c, d in jobs if not ((d != today and (c["id"], d) in _days)
                   or (d == today and (c["id"], d) in _today and time.time() - _today[(c["id"], d)][0] < TODAY_TTL)))
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        lists = list(ex.map(lambda j: _safe_list(nvr, j[0], j[1], today), jobs))
    seen, out = set(), []
    for lst in lists:
        for e in lst:
            if e["start"] < cutoff or e["event_id"] in seen:
                continue
            seen.add(e["event_id"])
            out.append(e)
    out.sort(key=lambda e: e["start"], reverse=True)
    return {"nvr": nvr, "events": out[:cap], "read": cold, "kept": len(jobs) - cold,
            "time_zone": _tz["name"], "cameras": len(cams), "chosen": bool(want)}


def keep_ready(hours=72):
    """Note that someone is looking, and keep today's lists fresh in the background
    while they are — so the next open is answered from memory. One thread, which
    stops by itself WATCH_FOR seconds after the last look."""
    with _lock:
        _watch["last_ask"] = time.time()
        _watch["hours"] = max(_watch["hours"], int(hours or 24))
        t = _watch["thread"]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_watch_loop, name="protect-events", daemon=True)
        _watch["thread"] = t
    t.start()


def _watch_loop():
    while time.time() - _watch["last_ask"] < WATCH_FOR:
        time.sleep(TODAY_TTL)
        try:
            events(_watch["hours"], 5000)
        except Exception:  # noqa: BLE001
            pass


def _safe_list(nvr, cam, day, today):
    try:
        return _list_for(nvr, cam, day, today)
    except Exception:  # noqa: BLE001 — one camera not answering never empties the rest
        return []


# ── pictures: fetched once, shrunk once, kept ──────────────────────────────
def _thumb_dir():
    # 696c: a new folder — 1.0.838–840 kept 90x90 copies of UniFi's 360x360 pictures in
    # "protect_thumbs"; that folder is dropped once, so no fuzzy copy is ever served again.
    old = os.path.join(_data_dir, "protect_thumbs")
    if os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    p = os.path.join(_data_dir, "protect_pictures")
    os.makedirs(p, exist_ok=True)
    return p


def jpeg_size(jpeg: bytes):
    """(width, height) from a JPEG's frame header, or None."""
    i, n = 2, len(jpeg or b"")
    if n < 4 or jpeg[:2] != b"\xff\xd8":
        return None
    while i + 9 < n:
        if jpeg[i] != 0xFF:
            i += 1
            continue
        m = jpeg[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7 or m == 0xFF:
            i += 1 if m == 0xFF else 2
            continue
        seg = int.from_bytes(jpeg[i + 2:i + 4], "big")
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(jpeg[i + 7:i + 9], "big"), int.from_bytes(jpeg[i + 5:i + 7], "big")
        i += 2 + seg
    return None


def scale_for(size):
    """The libjpeg-turbo scale for a picture of this size, or None to leave it alone:
    the most it can be shrunk (½, ¼ or ⅛) while its long side stays at least LONG_SIDE."""
    if not size:
        return None
    long_side = max(size)
    if long_side <= SHRINK_ABOVE:
        return None
    for d in (8, 4, 2):
        if long_side / d >= LONG_SIDE:
            return "1/%d" % d
    return None


def shrink(jpeg: bytes) -> bytes:
    """A large picture made smaller by libjpeg-turbo's DCT scaling (see scale_for); a picture
    that is already small comes back untouched. Returns the original when the tool is missing
    or the picture will not decode."""
    dj, cj = shutil.which("djpeg"), shutil.which("cjpeg")
    scale = scale_for(jpeg_size(jpeg))
    if not (dj and cj and jpeg and scale):
        return jpeg
    try:
        ppm = subprocess.run([dj, "-scale", scale], input=jpeg,
                             capture_output=True, timeout=10, check=True).stdout
        small = subprocess.run([cj, "-quality", "82", "-optimize", "-progressive"], input=ppm,
                               capture_output=True, timeout=10, check=True).stdout
        return small if small and len(small) < len(jpeg) else jpeg
    except Exception:  # noqa: BLE001
        return jpeg


def thumb(event_id: str):
    """(bytes, cached) — the event's picture, small. Raises ValueError on a bad id."""
    if not _EVENT_ID.match(str(event_id or "")):
        raise ValueError("not an event")
    path = os.path.join(_thumb_dir(), event_id + ".jpg")
    try:
        with open(path, "rb") as fh:
            return fh.read(), True
    except FileNotFoundError:
        pass
    with _lock:
        lk = _thumb_locks.setdefault(event_id, threading.Lock())
    with lk:                                   # two tiles asking for one picture fetch it once
        try:
            with open(path, "rb") as fh:
                return fh.read(), True
        except FileNotFoundError:
            pass
        nvr, _ = root()
        _ctype, raw = _get_bytes("/api/unifiprotect/thumbnail/%s/%s" % (nvr, event_id))
        small = shrink(raw)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(small)
        os.replace(tmp, path)
    with _lock:
        _thumb_locks.pop(event_id, None)
    _prune()
    return small, False


def _prune():
    try:
        d = _thumb_dir()
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jpg")]
        if len(files) <= THUMB_KEEP:
            return
        files.sort(key=lambda f: os.path.getmtime(f))
        for f in files[:len(files) - THUMB_KEEP]:
            os.remove(f)
    except Exception:  # noqa: BLE001
        pass


def shrink_available() -> bool:
    return bool(shutil.which("djpeg") and shutil.which("cjpeg"))
