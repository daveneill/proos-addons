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
    # REGISTER 1234 — lights, sensors and chimes (MEASURED on Dave's box: floodlights carry the light itself (unique id =
    # the MAC alone), light_motion, sensitivity, duration, paired_camera, status_light; sensors carry mount_type,
    # sensitivity, paired_camera, status_light, the temperature / humidity / light / motion / alarm reading switches and
    # clear_tamper)
    "_main_light": ("light", "Light", "Light", "turns this light on or off"),
    "light_motion": ("select", "Turns On", "Light", "changes when this light turns itself on"),
    "duration": ("number", "Stays On For", "Light", "changes how long it stays on"),
    "sensitivity": ("number", "Motion Sensitivity", "Sensing", "changes how easily motion sets it off"),
    "paired_camera": ("select", "Paired Camera", "Sensing", "changes which camera records when it triggers"),
    "mount_type": ("select", "Mounted On", "Sensing", "changes what the sensor reports as open and closed"),
    "temperature": ("switch", "Temperature Readings", "Readings", "changes what the sensor reports"),
    "humidity": ("switch", "Humidity Readings", "Readings", "changes what the sensor reports"),
    "light": ("switch", "Light Readings", "Readings", "changes what the sensor reports"),
    "alarm": ("switch", "Alarm Sound Readings", "Readings", "changes what the sensor listens for"),
    "clear_tamper": ("button", "Clear Tamper", "Sensing", "clears the tamper alert"),
    "volume": ("number", "Volume", "Chime", "changes how loud the chime rings"),
}
GROUPS = ("Picture & Recording", "On the Camera", "On-Screen", "Detections", "Doorbell", "Light", "Sensing", "Readings", "Chime")
# readings shown, never pressed: key → (label, kind of value)
READINGS = {"door": ("Door", "open"), "leak": ("Leak", "wet"), "motion": ("Motion", "seen"), "tampering": ("Tamper", "alert"),
            "battery_low": ("Battery Low", "alert"), "battery_level": ("Battery", "%"), "temperature_level": ("Temperature", "unit"),
            "humidity_level": ("Humidity", "unit"), "light_level": ("Light Level", "unit"), "dark": ("Dark", "yesno"),
            "alarm_sound": ("Alarm Sound", "text")}


def _mac(v):
    return re.sub(r"[^0-9a-f]", "", str(v or "").lower())


def _key_of(uid, m):
    """The control key in a platform unique id "<MAC>_<key>" for this MAC; the light itself is the MAC alone."""
    if _mac(uid[:12]) != m or len(re.sub(r"[^0-9A-Fa-f]", "", uid[:12])) != 12:
        return None
    if len(uid) == 12:
        return "_main_light"
    return uid[13:] if uid[12:13] == "_" else None


def readings_for(mac, entities, states):
    """What a light or sensor reports now (door, leak, motion, tamper, battery, temperature …) — shown, never pressed."""
    m = _mac(mac)
    st = {s.get("entity_id"): s for s in (states or []) if isinstance(s, dict)}
    out = []
    for e in (entities or []):
        if e.get("platform") != "unifiprotect" or e.get("disabled_by"):
            continue
        key = _key_of(str(e.get("unique_id") or ""), m)
        spec = READINGS.get(key or "")
        eid = str(e.get("entity_id") or "")
        if not spec or eid.split(".")[0] not in ("binary_sensor", "sensor"):
            continue
        s = st.get(eid) or {}
        v, a = s.get("state"), s.get("attributes") or {}
        if v in (None, "unavailable", "unknown"):
            words = "Not answering"
        elif spec[1] == "open":
            words = "Open" if v == "on" else "Closed"
        elif spec[1] == "wet":
            words = "Wet" if v == "on" else "Dry"
        elif spec[1] == "seen":
            words = "Detected" if v == "on" else "None"
        elif spec[1] == "alert":
            words = "Yes" if v == "on" else "No"
        elif spec[1] == "yesno":
            words = "Yes" if v == "on" else "No"
        elif spec[1] == "%":
            words = "%s%%" % v
        elif spec[1] == "unit":
            words = "%s%s" % (v, (" " + a["unit_of_measurement"]) if a.get("unit_of_measurement") else "")
        else:
            words = str(v)[:40]
        out.append({"key": key, "label": spec[0], "value": words,
                    "alert": spec[1] in ("open", "wet", "alert") and v == "on"})
    order = {k: i for i, k in enumerate(READINGS)}
    out.sort(key=lambda r: order.get(r["key"], 99))
    return out


def devices_view(devices, entities, states):
    """Lights, sensors or chimes (the official interface's list) as named fields, with their controls and readings."""
    out = []
    for d in (devices or []):
        if not isinstance(d, dict):
            continue
        ctl, dis = controls_for(d.get("mac"), entities, states)
        out.append({"id": d.get("id"), "name": d.get("name") or "Device", "model": d.get("type") or d.get("modelKey"),
                    "online": str(d.get("state") or "").upper() == "CONNECTED", "mac": d.get("mac"),
                    "controls": ctl, "controls_disabled": dis, "readings": readings_for(d.get("mac"), entities, states)})
    out.sort(key=lambda x: str(x["name"]).lower())
    return out


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
        key = _key_of(uid, m)
        if key is None:
            continue
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
    k = _key_of(uid, _mac(uid[:12]))
    spec = CONTROLS.get(k) if k else None
    eid = str(entity.get("entity_id") or "")
    if not spec or eid.split(".")[0] != spec[0]:
        return None, None, "ProOS does not offer that control"
    if entity.get("disabled_by"):
        return None, None, "the platform has that control switched off"
    a = (state or {}).get("attributes") or {}
    if spec[0] in ("switch", "light"):
        if not isinstance(value, bool):
            return None, None, "on or off"
        return spec[0], "turn_on" if value else "turn_off", {}
    if spec[0] == "button":
        if value is not True:
            return None, None, "press"
        return "button", "press", {}
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
    if domain == "button":
        return s not in (None, "unavailable", "unknown")          # a press claims nothing beyond the platform taking it
    if domain in ("switch", "light"):
        return s == ("on" if value else "off")
    if domain == "select":
        return s == str(value)
    try:
        return abs(float(s) - float(value)) < 1e-6
    except (TypeError, ValueError):
        return False
