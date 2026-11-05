"""PROTECT → DETECTIONS — register 1232 (the mockup Dave agreed; 6 Nov: "yes all of it please").

The console's own event list, as UniFi's own app reads it (/proxy/protect/api/events?start&end&limit — measured on
Dave's console 6 Nov: the last 24 h hit the 200 read limit; types motion, smartDetectZone, smartAudioDetect, sensor
opened/closed, light motion; smart types person, vehicle, animal, face …; every event answers a JPEG thumbnail).
NAMED FIELDS ONLY: what happened (in words), which device (by name, through the ONE device-name mechanism in
protectalarms), when, how long, how sure, and whether it has a picture.
"""
import re

from . import protectalarms as _pa

# how a detection reads to an installer; the console's own type names stay out of sight
KIND_WORDS = {"person": "Person", "vehicle": "Vehicle", "animal": "Animal", "package": "Package", "face": "Face",
              "licensePlate": "Number Plate", "motion": "Motion", "ring": "Doorbell Ring", "lightMotion": "Light Motion",
              "sensorOpened": "Opened", "sensorClosed": "Closed", "sensorMotion": "Sensor Motion",
              "sensorAlarm": "Sensor Alarm", "sensorWaterLeak": "Water Leak", "sensorExtremeValues": "Sensor Reading",
              "smoke": "Smoke Alarm", "cmonx": "CO Alarm", "alrmSmoke": "Smoke Alarm", "alrmCmonx": "CO Alarm",
              "alrmSiren": "Siren", "alrmBabyCry": "Baby Crying", "alrmSpeak": "Speaking", "alrmBark": "Barking",
              "alrmBurglar": "Burglar Alarm", "alrmCarHorn": "Car Horn", "alrmGlassBreak": "Glass Breaking"}
# the filter chips Pro offers, and which kinds each one covers
FILTERS = {"person": ("Person",), "vehicle": ("Vehicle", "Number Plate"), "animal": ("Animal",), "package": ("Package",),
           "face": ("Face",), "motion": ("Motion", "Light Motion", "Sensor Motion"),
           "sensor": ("Opened", "Closed", "Sensor Alarm", "Water Leak", "Sensor Reading"), "ring": ("Doorbell Ring",)}
_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]{6,64}$")
# 1232c — the console's own event types each filter asks for, so a filter reaches past a flood of motion (MEASURED on
# Dave's box: 235 of the newest 300 were motion, so "Person" only saw persons inside them). The view still keeps only
# the filter's kinds, so asking broadly never shows the wrong thing.
_SMART = ("smartDetectZone", "smartDetectLine")
CONSOLE_TYPES = {"person": _SMART, "vehicle": _SMART, "animal": _SMART, "package": _SMART, "face": _SMART,
                 "ring": ("ring",), "motion": ("motion", "lightMotion", "sensorMotion"),
                 "sensor": ("sensorOpened", "sensorClosed", "sensorAlarm", "sensorWaterLeak", "sensorExtremeValues")}


def console_query(start, end, only=None, limit=300):
    """The console's event-list query for a time window, narrowed to the filter's own types when there is one."""
    q = "/proxy/protect/api/events?start=%d&end=%d&limit=%d" % (start, end, limit)
    for t in CONSOLE_TYPES.get(only or "", ()):
        q += "&types=" + t
    return q


def _word(t):
    t = str(t or "")
    if t in KIND_WORDS:
        return KIND_WORDS[t]
    w = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t).strip()
    return " ".join(x[:1].upper() + x[1:] for x in w.split()) if re.match(r"^[A-Za-z ]{2,30}$", w) else None


def kinds(e):
    """What this event was, in words: its smart types first (Person, Vehicle …), else its own type."""
    out = []
    for t in (e.get("smartDetectTypes") or []):
        w = _word(t)
        if w and w not in out:
            out.append(w)
    if not out:
        w = _word(e.get("type"))
        if w and w not in ("Smart Detect Zone", "Smart Audio Detect", "Smart Detect Line"):
            out.append(w)
    return out or ["Activity"]


_SKIP = ("id", "user", "userId", "users", "thumbnail", "heatmap")


def _idlike(v):
    """An id or a MAC: 12+ hexadecimal characters once colons and dashes go — never a word like "sensorClosed"."""
    k = _pa._key(v) if isinstance(v, str) else ""
    return len(k) >= 12 and re.fullmatch(r"[0-9a-f]+", k) is not None


def _id_values(o, depth=0, path=""):
    """Every id-like value (as one spelling) up to three levels into an event, skipping its own id and any user."""
    out = []
    if depth > 3:
        return out
    if isinstance(o, dict):
        for k, v in o.items():
            if k in _SKIP:
                continue
            out += _id_values(v, depth + 1, path + "." + str(k))
    elif isinstance(o, list):
        for v in o[:20]:
            out += _id_values(v, depth + 1, path)
    elif _idlike(o):
        out.append(_pa._key(o))
    return out


def id_paths(o, depth=0, path=""):
    """For the box check only: the FIELD PATHS (never the values) that hold id-like values in an event."""
    out = []
    if depth > 3:
        return out
    if isinstance(o, dict):
        for k, v in o.items():
            if k in _SKIP:
                continue
            out += id_paths(v, depth + 1, path + "." + str(k) if path else str(k))
    elif isinstance(o, list):
        for v in o[:20]:
            out += id_paths(v, depth + 1, path + "[]")
    elif _idlike(o):
        out.append(path)
    return out


def safe_id(v):
    return bool(v) and bool(_SAFE_ID.match(str(v)))


def events_view(events, bootstrap=None, only=None):
    """Each detection as named fields only, newest first. only = a FILTERS key, or None for all."""
    names = _pa.device_names(bootstrap)
    want = set(FILTERS.get(only, ())) if only else None
    out = []
    for e in (events or []):
        if not isinstance(e, dict) or not safe_id(e.get("id")):
            continue
        k = kinds(e)
        if want is not None and not (want & set(k)):
            continue
        dev = None
        for f in ("camera", "sensor", "light", "doorlock", "chime"):
            if e.get(f):
                dev = names.get(_pa._key(e.get(f)))
                if dev:
                    break
        if not dev:
            # 1232b/c — MEASURED on Dave's box: light-motion, door opened/closed and Access events came back unnamed —
            # they name their device elsewhere (1232b's top-level scan did not find it: 30 still unnamed). Any value up
            # to three levels into the event that is EXACTLY a known device's id or MAC names it (never the event's own
            # id, never a user, never a word).
            dev = next((names[k] for k in _id_values(e) if k in names), None)
        start, end = e.get("start"), e.get("end")
        out.append({"id": e["id"], "kinds": k, "device": dev or "Unnamed device", "start": start,
                    "seconds": int((end - start) / 1000) if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start else None,
                    "score": e.get("score") if isinstance(e.get("score"), (int, float)) else None,
                    "picture": bool(e.get("thumbnail")) or e.get("type") in ("smartDetectZone", "motion", "ring", "smartDetectLine")})
    out.sort(key=lambda x: x["start"] or 0, reverse=True)
    return out


def counts(view):
    """{filter key: how many} over a view — for the chips."""
    c = {k: 0 for k in FILTERS}
    for x in view:
        for k, ws in FILTERS.items():
            if set(ws) & set(x["kinds"]):
                c[k] += 1
    return c
