"""PROTECT → CAMERAS (and the Doorbell) — register 1233 (the mockup Dave agreed; 6 Nov: "yes all of it please").

PLATFORM FIRST (CLAUDE.md law): every camera setting ProOS changes is the PLATFORM's own UniFi Protect control — a
switch, select or number the unifiprotect integration already makes, named "<camera MAC>_<key>" (MEASURED on Dave's
box 6 Nov: status_light 11, privacy_mode 11, osd_name/date/logo/bitrate 12, motion 13, smart_person/vehicle/animal 8,
recording_mode 12, hdr_mode 11, infrared 11, mic_level 12, doorbell_text 1, doorbell_ring_volume 1 …). ProOS joins each
camera to its own controls, says what each will do, asks first (in Pro), and reads the platform's state back.
Only the controls listed here are offered; a disabled one is counted, never pressed.
"""
import re

# key → (domain, label, group, what it does when changed — for the question Pro asks)
CONTROLS = {
    "recording_mode": ("select", "Recording", "Picture & Recording", "changes when this camera records"),
    "hdr_mode": ("select", "HDR", "Picture & Recording", "changes the picture's HDR"),
    "infrared": ("select", "Night Vision", "Picture & Recording", "changes the night vision"),
    "privacy_mode": ("switch", "Privacy Mode", "Picture & Recording", "privacy mode blanks this camera's picture and stops its recording"),
    "high_fps": ("switch", "High Frame Rate", "Picture & Recording", "changes the frame rate"),
    "status_light": ("switch", "Status Light", "On the Camera", "changes the light on the camera"),
    "mic_level": ("number", "Microphone Level", "On the Camera", "changes how loud the microphone records"),
    "osd_name": ("switch", "Show Name", "On-Screen", "changes what is printed on the picture"),
    "osd_date": ("switch", "Show Date", "On-Screen", "changes what is printed on the picture"),
    "osd_logo": ("switch", "Show Logo", "On-Screen", "changes what is printed on the picture"),
    "osd_bitrate": ("switch", "Show Bitrate", "On-Screen", "changes what is printed on the picture"),
    "motion": ("switch", "Motion Detection", "Detections", "changes what this camera reports and records on"),
    "smart_person": ("switch", "Person", "Detections", "changes what this camera reports and records on"),
    "smart_vehicle": ("switch", "Vehicle", "Detections", "changes what this camera reports and records on"),
    "smart_animal": ("switch", "Animal", "Detections", "changes what this camera reports and records on"),
    "smart_package": ("switch", "Package", "Detections", "changes what this camera reports and records on"),
    "smart_licenseplate": ("switch", "Number Plate", "Detections", "changes what this camera reports and records on"),
    "smart_smoke": ("switch", "Smoke Alarm Sound", "Detections", "changes what this camera listens for"),
    "smart_cmonx": ("switch", "CO Alarm Sound", "Detections", "changes what this camera listens for"),
    "smart_siren": ("switch", "Siren Sound", "Detections", "changes what this camera listens for"),
    "smart_baby_cry": ("switch", "Baby Crying", "Detections", "changes what this camera listens for"),
    "smart_speak": ("switch", "Speaking", "Detections", "changes what this camera listens for"),
    "smart_bark": ("switch", "Barking", "Detections", "changes what this camera listens for"),
    "smart_car_alarm": ("switch", "Car Alarm", "Detections", "changes what this camera listens for"),
    "smart_car_horn": ("switch", "Car Horn", "Detections", "changes what this camera listens for"),
    "smart_glass_break": ("switch", "Glass Breaking", "Detections", "changes what this camera listens for"),
    "doorbell_text": ("select", "Doorbell Message", "Doorbell", "changes the message on the doorbell's screen"),
    "doorbell_ring_volume": ("number", "Ring Volume", "Doorbell", "changes how loud the doorbell rings"),
}
GROUPS = ("Picture & Recording", "On the Camera", "On-Screen", "Detections", "Doorbell")


def _mac(v):
    return re.sub(r"[^0-9a-f]", "", str(v or "").lower())


def controls_for(mac, entities, states):
    """The platform's own controls for one camera (by its MAC), with their state now. Disabled ones are counted."""
    m = _mac(mac)
    st = {s.get("entity_id"): s for s in (states or []) if isinstance(s, dict)}
    out, disabled = [], 0
    if len(m) != 12:
        return out, disabled
    for e in (entities or []):
        if e.get("platform") != "unifiprotect":
            continue
        uid = str(e.get("unique_id") or "")
        if _mac(uid[:12]) != m or uid[12:13] != "_":
            continue
        key = uid[13:]
        spec = CONTROLS.get(key)
        eid = str(e.get("entity_id") or "")
        if not spec or eid.split(".")[0] != spec[0]:
            continue
        if e.get("disabled_by"):
            disabled += 1
            continue
        s = st.get(eid) or {}
        a = s.get("attributes") or {}
        row = {"entity_id": eid, "key": key, "label": spec[1], "group": spec[2], "domain": spec[0], "does": spec[3],
               "state": s.get("state"), "available": s.get("state") not in (None, "unavailable", "unknown")}
        if spec[0] == "select":
            row["options"] = [str(o) for o in (a.get("options") or [])][:40]
        if spec[0] == "number":
            row.update({"min": a.get("min"), "max": a.get("max"), "step": a.get("step"), "unit": a.get("unit_of_measurement")})
        out.append(row)
    order = {k: i for i, k in enumerate(CONTROLS)}
    out.sort(key=lambda r: order.get(r["key"], 99))
    return out, disabled


def cameras_view(cameras, entities, states):
    """Each camera (the official interface's list) as named fields, with its platform controls."""
    out = []
    for c in (cameras or []):
        if not isinstance(c, dict):
            continue
        ff = c.get("featureFlags") or {}
        ctl, dis = controls_for(c.get("mac"), entities, states)
        out.append({"id": c.get("id"), "name": c.get("name") or "Camera", "model": c.get("type") or c.get("modelKey"),
                    "online": str(c.get("state") or "").upper() == "CONNECTED", "mac": c.get("mac"),
                    "video_mode": c.get("videoMode"), "has_mic": bool(ff.get("hasMic")), "has_speaker": bool(ff.get("hasSpeaker")),
                    "smart": [str(t) for t in (ff.get("smartDetectTypes") or [])],
                    "doorbell": any(r["group"] == "Doorbell" for r in ctl),
                    "controls": ctl, "controls_disabled": dis})
    out.sort(key=lambda x: str(x["name"]).lower())
    return out


def plan(entity, state, value):
    """(domain, service, data) to set one of the platform's camera controls, or (None, None, why). The entity must be one
    of CONTROLS on the unifiprotect platform, enabled; a select's option must be one it offers; a number in its range."""
    if not entity or entity.get("platform") != "unifiprotect":
        return None, None, "that is not one of the platform's UniFi Protect controls"
    uid = str(entity.get("unique_id") or "")
    spec = CONTROLS.get(uid[13:]) if len(_mac(uid[:12])) == 12 and uid[12:13] == "_" else None
    eid = str(entity.get("entity_id") or "")
    if not spec or eid.split(".")[0] != spec[0]:
        return None, None, "ProOS does not offer that control"
    if entity.get("disabled_by"):
        return None, None, "the platform has that control switched off"
    a = (state or {}).get("attributes") or {}
    if spec[0] == "switch":
        if not isinstance(value, bool):
            return None, None, "on or off"
        return "switch", "turn_on" if value else "turn_off", {}
    if spec[0] == "select":
        if str(value) not in [str(o) for o in (a.get("options") or [])]:
            return None, None, "that is not one of its choices"
        return "select", "select_option", {"option": str(value)}
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, None, "a number"
    lo, hi = a.get("min"), a.get("max")
    if (lo is not None and v < float(lo)) or (hi is not None and v > float(hi)):
        return None, None, "outside its range"
    return "number", "set_value", {"value": v}


def landed(domain, state, value):
    """Whether the platform's state now shows the value asked for."""
    s = (state or {}).get("state")
    if domain == "switch":
        return s == ("on" if value else "off")
    if domain == "select":
        return s == str(value)
    try:
        return abs(float(s) - float(value)) < 1e-6
    except (TypeError, ValueError):
        return False
