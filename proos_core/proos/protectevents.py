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
