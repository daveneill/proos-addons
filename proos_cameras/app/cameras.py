"""ProOS Cameras — UniFi Protect cameras in Apple Home, published by ProOS. Register 970s, stage A.

Dave, 22 Oct 2026, 20:19 — "2 do it all properly please": ProOS publishes the cameras itself so they can carry
HomeKit Secure Video (the Home app's People / Animals / Vehicles / Packages), the doorbell and two-way talk — none of
which the platform's Apple Home integration can do (it states HKSV is unsupported; its library has no HomeKit Data
Stream). Plan: docs/ProOS_Apple_Home_Accessories_And_Camera_Alerts_Plan_2026-10-22.md §6.

STAGE A (this file): each camera chosen in Pro is its own Apple Home accessory with its own pairing code — live view
(Protect's own RTSPS stream through ffmpeg to the iPhone, SRTP, the same command shape the platform's Apple Home
cameras use), snapshots (Protect's own snapshot) and motion (the camera's own motion event from the platform).

What is the platform's and stays so: the camera list, each camera's device, MAC, motion event and doorbell event
(its registries and states, read through Core's own connection code); Protect's address and key (its integration's
entry). What is ProOS's: the accessories, their pairing codes (/data), and the media path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import socket
import threading
import time
import urllib.request

import protect
import talk

log = logging.getLogger("proos_cameras")

BASE = os.environ.get("PROOS_HA_BASE", "http://supervisor/core")
DATA = os.environ.get("PROOS_DATA", "/data")
PORT0 = 51900                    # first Apple Home port; one per camera, in order (plumbing)
EVENT_PULSE = 1.0                # s an event-type motion is held "detected" for Apple (plumbing)

MOTION_KINDS = ["motion", "smart_obj_person", "smart_obj_vehicle", "smart_obj_animal"]   # Protect's registry ids
RESOLUTIONS = [(320, 180), (320, 240), (480, 270), (480, 360), (640, 360), (640, 480), (1024, 576), (1024, 768),
               (1280, 720), (1280, 960), (1920, 1080), (1600, 1200)]      # the platform's own list (type_cameras)

# 970x: the Homebridge UniFi Protect plugin's tables (homebridge-unifi-protect 8.1.0, media/resolution.js)
RESOLUTIONS_4X3 = [(3840, 2880), (2560, 1920), (1920, 1440), (1280, 960), (1024, 768), (640, 480), (480, 360), (320, 240)]
RESOLUTIONS_16X9 = [(3840, 2160), (2560, 1440), (1920, 1080), (1280, 720), (640, 360), (480, 270), (320, 180)]
MAX_CONVERTED_PIXELS = 1920 * 1440   # ProOS's own cap for a converted picture (970x) — the plugin mostly copies


def is_4x3(w: int, h: int) -> bool:
    """As the plugin: a portrait camera (taller than wide) counts as its landscape family. One stated difference: within
    2 % of 4:3 counts (the plugin wants it exact) — Front Door's main stream is 3024 x 4096 (0.738, not 0.75), which
    the exact test files as wide and caps the iPhone at 1080 high instead of 1440."""
    return abs(max(w, h) * 3 - min(w, h) * 4) <= 0.02 * min(w, h) * 4


def advertised(native: dict | None, converted: bool) -> list:
    """The sizes offered to the iPhone, as [w, h, fps].
    Copied cameras (H.264, or unread) keep the platform's list at 30 fps, as before. A converted camera gets the
    plugin's list: its own size, then its aspect table — every size under its longest side plus the 1920 / 1280
    widths Apple requires — at the camera's own frame rate (15 / 24 / 30, as the plugin normalises); then ProOS's cap
    drops any size above 1920 x 1440 so the box is never asked to convert 4K live."""
    if not converted or not native or not native.get("width") or not native.get("height"):
        return [[w, h, 30] for w, h in RESOLUTIONS]
    nw, nh = int(native["width"]), int(native["height"])
    f = float(native.get("fps") or 15)
    fps = 30 if f > 24 else 24 if f > 15 else 15
    out = [[nw, nh, fps]]
    top = max(nw, nh)
    for w, h in (RESOLUTIONS_4X3 if is_4x3(nw, nh) else RESOLUTIONS_16X9):
        if (max(w, h) < top or w in (1920, 1280)) and [w, h, fps] not in out:
            out.append([w, h, fps])
            top = max(top, max(w, h))
    return [r for r in out if r[0] * r[1] <= MAX_CONVERTED_PIXELS]


def probe_stream(src: str, run=None) -> dict:
    """What Protect actually streams — codec, width, height, frame rate — read once with ffprobe ({} when unread)."""
    import subprocess
    run = run or subprocess.run
    try:
        r = run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height,avg_frame_rate", "-of", "json", src],
                capture_output=True, text=True, timeout=12)
        st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
        num, _, den = str(st.get("avg_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        return {"codec": str(st.get("codec_name") or "").lower(), "width": int(st.get("width") or 0),
                "height": int(st.get("height") or 0), "fps": round(fps, 2)}
    except Exception:  # noqa: BLE001
        return {}


# ── what the platform says about each camera ─────────────────────────────────────────────────────────────────────
def plan(wanted, entities: list, devices: list, states: dict) -> list:
    """For each camera entity wanted: {entity, mac, name, model, motion, doorbell}. Read by fact — the camera's
    device, Protect's MAC in its registry id (<MAC>_0), and the device's event entities by their class."""
    by_dev = {}
    for e in entities or []:
        by_dev.setdefault(e.get("device_id"), []).append(e)
    devs = {d.get("id"): d for d in devices or []}
    out = []
    for eid in wanted or []:
        e = next((x for x in entities or [] if x.get("entity_id") == eid), None)
        if not e or e.get("platform") != "unifiprotect" or not eid.startswith("camera."):
            continue
        mac = protect.norm_mac(str(e.get("unique_id") or "").split("_")[0])
        if len(mac) != 12:
            continue
        d = devs.get(e.get("device_id")) or {}
        same = by_dev.get(e.get("device_id"), [])

        def ev(cls):
            for x in same:
                xid = x.get("entity_id", "")
                if xid.startswith("event.") and not x.get("disabled_by") and (
                        (x.get("original_device_class") or x.get("device_class")) == cls
                        or ((states.get(xid) or {}).get("attributes") or {}).get("device_class") == cls):
                    return xid
            return None
        def by_uid(domain, suffixes):
            """970aa: the device's entities by Protect's own registry id (<MAC>_<kind>), never by name."""
            found = []
            for x in same:
                xid, uid = x.get("entity_id", ""), str(x.get("unique_id") or "")
                pre, _, kind = uid.partition("_")
                if xid.startswith(domain) and not x.get("disabled_by") and protect.norm_mac(pre) == mac \
                        and kind in suffixes:
                    found.append(xid)
            return sorted(found, key=lambda i: suffixes.index(str(next(
                (y.get("unique_id") for y in same if y.get("entity_id") == i), "_")).partition("_")[2]))
        pkg = by_uid("event.", ["package"])
        out.append({"entity": eid, "mac": mac, "name": d.get("name_by_user") or d.get("name") or eid,
                    "model": d.get("model") or "UniFi Protect", "sw": d.get("sw_version") or "",
                    "motion": ev("motion"), "doorbell": ev("doorbell"),
                    # 970aa: Protect's on/off detections — held for as long as Protect says (Front Door, 06:28:
                    # person detected 25 s while the plain motion event never fired), and the package event
                    "motion_sensors": by_uid("binary_sensor.", MOTION_KINDS), "package": pkg[0] if pkg else None})
    return out


# ── pairing codes ────────────────────────────────────────────────────────────────────────────────────────────────
_TRIVIAL = {"000-00-000", "111-11-111", "222-22-222", "333-33-333", "444-44-444", "555-55-555", "666-66-666",
            "777-77-777", "888-88-888", "999-99-999", "123-45-678", "876-54-321"}    # Apple refuses these


def new_code(rng=random.SystemRandom()) -> str:
    while True:
        d = "%08d" % rng.randrange(0, 10 ** 8)
        c = "%s-%s-%s" % (d[:3], d[3:5], d[5:])
        if c not in _TRIVIAL:
            return c


def codes(path: str = None) -> dict:
    path = path or os.path.join(DATA, "pincodes.json")
    try:
        with open(path) as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def code_for(mac: str, path: str = None) -> str:
    path = path or os.path.join(DATA, "pincodes.json")
    c = codes(path)
    if mac not in c:
        c[mac] = new_code()
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(c, fh)
        os.replace(tmp, path)
    return c[mac]


def notice_id(mac: str) -> str:
    return "proos_cameras_%s" % mac.lower()


def notice_message(name: str, code: str) -> str:
    """The platform's own HomeKit pairing notice wording, so Pro reads it the same way (`ahParsePairing`)."""
    return ("To set up %s in the Home App, scan the QR code or enter the following code:\n### %s\n"
            "Published by ProOS Cameras." % (name, code))


# ── the platform, through Core's own connection code ────────────────────────────────────────────────────────────
def _token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN", "")


def ha_rest(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw.decode()) if raw else {}


def notify(mac: str, name: str, code: str, paired: bool) -> None:
    try:
        if paired:
            ha_rest("POST", "/api/services/persistent_notification/dismiss", {"notification_id": notice_id(mac)})
        else:
            ha_rest("POST", "/api/services/persistent_notification/create",
                    {"notification_id": notice_id(mac), "title": "HomeKit Pairing",
                     "message": notice_message(name, code)})
    except Exception as e:  # noqa: BLE001
        log.warning("pairing notice for %s not written: %s", name, e)


# ── the media path (live view) ───────────────────────────────────────────────────────────────────────────────────
PROFILE_NAMES = ["baseline", "main", "high"]     # the platform's DEFAULT_VIDEO_PROFILE_NAMES
LEVEL_NAMES = ["3.1", "3.2", "4.0"]              # HAP's H.264 level codes 0 / 1 / 2


def probe_codec(src: str, run=None) -> str:
    """What Protect actually streams: 'h264', 'hevc', … ('' when ffprobe cannot tell). Read, never assumed —
    970t addendum: a G6 doorbell's stream that is not H.264 never plays in Apple Home when copied."""
    import subprocess
    run = run or subprocess.run
    try:
        r = run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", src],
                capture_output=True, text=True, timeout=12)
        return (r.stdout or "").strip().splitlines()[0].strip().lower() if r.stdout else ""
    except Exception:  # noqa: BLE001
        return ""


def video_args(codec: str, s: dict) -> list:
    """H.264 is passed through untouched (as the platform does for Protect); anything else — Apple Home plays only
    H.264 — is made H.264 at the size, rate, bit rate, profile and level the iPhone asked for, with the settings of
    homebridge-plugin-utils 2.4.0 (the encoder behind the Homebridge UniFi Protect plugin): libx264 veryfast, the asked
    profile and level, no B-frames, one slice per frame.
    970v: the platform's "-tune zerolatency" was used until 0.2.4 — it cuts every frame into several slices, which
    ffmpeg then bundles several to a packet (STAP-A, 164 of 465 packets measured); the Home app showed nothing for
    30 s of a valid, correctly encrypted stream. The Homebridge encoder never uses it."""
    if codec in ("h264", ""):
        return ["-c:v", "copy"]
    try:
        prof = PROFILE_NAMES[int.from_bytes(s.get("v_profile_id") or b"\x02", "big")]
    except (IndexError, TypeError):
        prof = "high"
    try:
        level = LEVEL_NAMES[int.from_bytes(s.get("v_level") or b"\x02", "big")]
    except (IndexError, TypeError):
        level = "4.0"
    br = int(s.get("v_max_bitrate") or 2000)
    fps = int(s.get("fps") or 30)
    # a key frame every second (with its headers), so the iPhone has a picture within a second of the first frame
    # rather than at x264's default ~8 s — longer than Apple waits (970t: measured 9.4 s before, on a 6 s-GOP source)
    return ["-c:v", "libx264", "-profile:v", prof, "-level:v", level, "-preset", "veryfast", "-bf", "0", "-pix_fmt",
            "yuv420p", "-vf", fit(int(s.get("width") or 1280), int(s.get("height") or 720)),
            "-r", str(fps), "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0",
            "-b:v", "%dk" % br, "-bufsize", "%dk" % (br * 4), "-maxrate", "%dk" % br]


def fit(w: int, h: int) -> str:
    """The camera's picture at the HEIGHT the iPhone asked for, in its own true shape, with square pixels — the
    Homebridge UniFi Protect plugin's scaling (homebridge-plugin-utils 2.4.0: "scale=-2:min(ih, height)"); the Home
    app shows a tall doorbell tall. The asked width is not forced (w is kept for the call shape only).
    970t: forcing 1280 x 720 squashed Front Door's 3024 x 4096 portrait (SAR 3:4). 970w: 0.2.4–0.2.5 fitted it
    inside 1280 x 720 with black bars — it played (Dave, 23:10) but "really small"."""
    return "scale=-2:min(ih\\,%d),setsar=1" % h


def ffmpeg_live(src: str, s: dict, audio_port: int | None, codec: str = "h264") -> list:
    """The ffmpeg command for one live view: Protect's stream in, video as SRTP to the iPhone (copied when it is
    H.264, made H.264 when not), sound as Opus to the relay — the platform's own command shape (type_cameras
    VIDEO_OUTPUT / AUDIO_OUTPUT)."""
    # the stream is already known (probed), so ffmpeg is not left to study it for its default ~5 s before starting
    fast = [] if codec in ("h264", "") else ["-fflags", "nobuffer", "-probesize", "500000", "-analyzeduration", "500000"]
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning"] + fast + ["-rtsp_transport", "tcp", "-i", src,
           "-map", "0:v:0", "-an"] + video_args(codec, s) + [
           "-payload_type", "99", "-ssrc", str(s["v_ssrc"]), "-f", "rtp",
           "-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80", "-srtp_out_params", s["v_srtp_key"],
           "srtp://%s:%d?rtcpport=%d&localrtpport=%d&pkt_size=%d"
           % (s["address"], s["v_port"], s["v_port"], s["v_port"], 1316)]
    if audio_port:
        rate = int(s.get("a_sample_rate") or 16)
        br = int(s.get("a_max_bitrate") or 24)
        cmd += ["-map", "0:a:0?", "-vn", "-c:a", "libopus", "-application", "lowdelay", "-ac", "1",
                "-ar", "%dk" % rate, "-b:a", "%dk" % br, "-bufsize", "%dk" % (br * 4),
                "-frame_duration", str(int(s.get("a_packet_time") or 20)), "-payload_type", "110",
                "-ssrc", str(s["a_ssrc"]), "-f", "rtp", "rtp://127.0.0.1:%d?pkt_size=188" % audio_port]
    return cmd


def make_accessory_class():
    """Built late so the module (and its pure functions above) import without the Apple Home library."""
    from pyhap.camera import Camera as PyhapCamera
    from pyhap.camera import VIDEO_CODEC_PARAM_LEVEL_TYPES as LV, VIDEO_CODEC_PARAM_PROFILE_ID_TYPES as PF

    class ProOSCamera(PyhapCamera):
        def __init__(self, cam: dict, cid: str, driver, address: str, native: dict | None = None):
            native = native or {}
            converted = native.get("codec", "") not in ("h264", "")
            opts = {
                "video": {"codec": {"profiles": [PF["BASELINE"], PF["MAIN"], PF["HIGH"]],      # the platform's own
                                    "levels": [LV["TYPE3_1"], LV["TYPE3_2"], LV["TYPE4_0"]]},
                          "resolutions": advertised(native, converted)},      # 970x
                "audio": {"codecs": [{"type": "OPUS", "samplerate": 24}, {"type": "OPUS", "samplerate": 16}]},
                "srtp": True, "address": address, "stream_count": 2,
            }
            super().__init__(opts, driver, cam["name"])
            self.cam, self.cid = cam, cid
            info = self.get_service("AccessoryInformation")
            info.configure_char("Manufacturer", value="Ubiquiti")
            info.configure_char("Model", value=str(cam.get("model") or "UniFi Protect")[:64])
            info.configure_char("SerialNumber", value=cam["mac"])
            if cam.get("sw"):
                info.configure_char("FirmwareRevision", value=str(cam["sw"])[:64])
            self.char_motion = None
            if cam.get("motion") or cam.get("motion_sensors") or cam.get("package"):
                self.char_motion = self.add_preload_service("MotionSensor").configure_char(
                    "MotionDetected", value=False)
            # 970t: a doorbell — the platform's own doorbell services (homekit/doorbell.py), and a Speaker that is
            # real here: the iPhone's voice reaches the door (talk.Relay → Protect's talk session).
            self.char_ring = self.char_ring_switch = None
            self.talking = {}
            self.codec = {cid: native["codec"]} if native.get("codec") else {}   # read at start-up (970x)
            if cam.get("doorbell") or cam.get("speaker"):
                from pyhap.const import CATEGORY_VIDEO_DOOR_BELL
                self.category = CATEGORY_VIDEO_DOOR_BELL
                if cam.get("doorbell"):
                    serv = self.add_preload_service("Doorbell")
                    self.set_primary_service(serv)
                    self.char_ring = serv.configure_char("ProgrammableSwitchEvent", value=0)
                    self.char_ring_switch = self.add_preload_service("StatelessProgrammableSwitch").configure_char(
                        "ProgrammableSwitchEvent", value=0, valid_values={"SinglePress": 0})
                self.add_preload_service("Speaker").configure_char("Mute", value=0)

        # the doorbell's ring — from the platform's own ring event for this camera
        def ring(self):
            if self.char_ring:
                self.char_ring.set_value(0)
                self.char_ring_switch.set_value(0)
                log.info("%s: ring → Apple Home", self.cam["name"])

        def _open_talk(self, sid):
            """Called by the relay on the iPhone's first word: Protect's talk session, then ffmpeg to the door."""
            import subprocess
            import tempfile
            sess = protect.talkback(self.cid)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            fd, sdp = tempfile.mkstemp(suffix=".sdp")
            with os.fdopen(fd, "w") as fh:
                fh.write(talk.talk_sdp(port))
            proc = subprocess.Popen(talk.ffmpeg_talk(sdp, sess), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.talking[sid] = (proc, sdp)
            log.info("%s: talking — the iPhone's voice to the door (%s, %s Hz)", self.cam["name"],
                     sess.get("codec"), sess.get("samplingRate"))
            return port

        # motion — from the platform's own motion event for this camera
        def motion(self):
            if not self.char_motion:
                return
            self.char_motion.set_value(True)
            # a pulse never ends motion that Protect still holds on (970aa)
            threading.Timer(EVENT_PULSE, lambda: None if getattr(self, "held", False)
                            else self.char_motion.set_value(False)).start()
            log.info("%s: motion → Apple Home", self.cam["name"])

        def motion_state(self, on: bool):
            """970aa: motion held on while any of Protect's detections is on."""
            self.held = bool(on)
            if self.char_motion and bool(self.char_motion.value) != bool(on):
                self.char_motion.set_value(bool(on))
                log.info("%s: motion %s → Apple Home", self.cam["name"], "started" if on else "ended")

        def get_snapshot(self, image_size):  # noqa: ARG002 — Apple's size is a hint; Protect's own picture is used
            return protect.snapshot(self.cid)

        async def start_stream(self, session_info, stream_config):
            try:
                src = protect.stream_url(self.cid, "high")
            except Exception as e:  # noqa: BLE001
                log.error("%s: no stream from Protect: %s", self.cam["name"], e)
                return False
            sid = session_info.get("id")
            can_talk = bool(self.char_ring or self.cam.get("speaker"))
            proxy = talk.Relay(stream_config["address"], stream_config["a_port"], stream_config["a_srtp_key"],
                               int(stream_config.get("a_sample_rate") or 16),
                               on_voice=(lambda: self._open_talk(sid)) if can_talk else None)
            proxy.start()
            port = proxy.local_port
            if self.cid not in self.codec:
                self.codec[self.cid] = await asyncio.get_running_loop().run_in_executor(None, probe_codec, src)
                log.info("%s: Protect streams %s — %s", self.cam["name"], self.codec[self.cid] or "an unread codec",
                         "passed through" if self.codec[self.cid] in ("h264", "") else "made H.264 for Apple Home")
            cmd = ffmpeg_live(src, stream_config, port, self.codec[self.cid])
            # 970t: what the iPhone asked for — read, so a picture that never shows can be explained from the log
            log.info("%s: the iPhone asked for %sx%s at %s fps, profile %s, level %s, up to %s kbit/s, video type %s, "
                     "packets up to %s bytes", self.cam["name"], stream_config.get("width"), stream_config.get("height"),
                     stream_config.get("fps"), stream_config.get("v_profile_id"), stream_config.get("v_level"),
                     stream_config.get("v_max_bitrate"), stream_config.get("v_payload_type"),
                     stream_config.get("v_max_mtu"))
            cmd = cmd[:1] + ["-progress", "pipe:1", "-stats_period", "3"] + cmd[1:]
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            session_info["process"], session_info["proxy"] = proc, proxy

            async def _errors(stream, name=self.cam["name"]):      # ffmpeg's own words, into the add-on's log
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    log.warning("%s: ffmpeg: %s", name, line.decode(errors="replace").rstrip()[:300])
            session_info["errlog"] = asyncio.ensure_future(_errors(proc.stderr))

            async def _progress(stream, name=self.cam["name"]):     # frames actually sent, every 3 s
                frame = None
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    k, _, v = line.decode(errors="replace").strip().partition("=")
                    if k == "frame":
                        frame = v
                    elif k == "progress" and frame is not None:
                        log.info("%s: video frames sent so far: %s", name, frame)
                rc = await proc.wait()
                log.info("%s: ffmpeg ended (exit %s)", name, rc)
            session_info["progress"] = asyncio.ensure_future(_progress(proc.stdout))
            log.info("%s: live view started for %s", self.cam["name"], stream_config["address"])
            return True

        async def stop_stream(self, session_info):
            proc, proxy = session_info.pop("process", None), session_info.pop("proxy", None)
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    proc.kill()
            if proxy:
                proxy.stop()
                log.info("%s: live view stopped (sound out %d packets, voice to the door %d)", self.cam["name"],
                         proxy.packets_out, proxy.packets_back)
            t = self.talking.pop(session_info.get("id"), None)
            if t:
                t[0].terminate()
                try:
                    os.unlink(t[1])
                except OSError:
                    pass

        async def reconfigure_stream(self, session_info, stream_config):
            return True

    return ProOSCamera


# ── running ──────────────────────────────────────────────────────────────────────────────────────────────────────
class Published:
    def __init__(self, cam, cid, index):
        self.cam, self.cid, self.index = cam, cid, index
        self.acc = self.driver = None
        self.loop = asyncio.new_event_loop()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="hap-" + self.cam["mac"]).start()

    def _run(self):
        from pyhap.accessory_driver import AccessoryDriver
        asyncio.set_event_loop(self.loop)
        if self.cam.get("secure_video") and self._run_secure_video():
            return
        code = code_for(self.cam["mac"])
        self.driver = AccessoryDriver(port=PORT0 + self.index, persist_file=os.path.join(
            DATA, "%s.state" % self.cam["mac"]), pincode=code.encode(), loop=self.loop)
        native = {}
        try:      # 970x: read once what Protect streams, so the sizes offered to the iPhone are this camera's own
            native = probe_stream(protect.stream_url(self.cid, "high"))
        except Exception as e:  # noqa: BLE001
            log.warning("%s: stream not read at start-up (%s) — the platform's sizes offered", self.cam["name"], e)
        if not native.get("codec"):
            log.warning("%s: stream not read at start-up — the platform's sizes offered", self.cam["name"])
        else:
            log.info("%s: Protect streams %s %sx%s at %s fps — %s", self.cam["name"], native["codec"],
                     native.get("width"), native.get("height"), native.get("fps"),
                     "passed through" if native["codec"] == "h264" else "made H.264 for Apple Home")
        self.acc = make_accessory_class()(self.cam, self.cid, self.driver, self.driver.state.address, native)
        log.info("%s: sizes offered to the iPhone: %s", self.cam["name"],
                 ", ".join("%dx%d@%d" % tuple(r) for r in advertised(native, native.get("codec", "") not in ("h264", ""))))
        self.driver.add_accessory(self.acc)
        log.info("%s published on port %d (%s)", self.cam["name"], PORT0 + self.index,
                 "paired" if self.driver.state.paired else "waiting to be paired")
        self.driver.start()


    def _run_secure_video(self) -> bool:
        """970y: the camera as Apple's new Secure Video camera — its own accessory (its own state file, so it is added
        again in the Home app). False → the classic camera instead (its channels could not be read)."""
        from pyhap.accessory_driver import AccessoryDriver
        from securevideo import accessory as SA
        from securevideo import datastream as SD
        name = self.cam["name"]
        chans = SA.channels_for(protect, self.cid, name)
        if not chans:
            log.error("%s: no channel could be read — published as the classic camera instead", name)
            return False
        top = chans[0]
        try:
            hz = SA.audio_rate(protect.stream_url(self.cid, "medium" if any(c["quality"] == "medium" for c in chans)
                                                  else top["quality"]))
        except Exception:  # noqa: BLE001
            hz = 0
        sizes = advertised({"codec": "hevc", "width": top["width"], "height": top["height"], "fps": top["fps"]}, True)
        SD.install_key_keeper()
        code = code_for(self.cam["mac"])
        self.driver = AccessoryDriver(port=PORT0 + self.index, persist_file=os.path.join(
            DATA, "%s.sv.state" % self.cam["mac"]), pincode=code.encode(), loop=self.loop)
        server = SD.Server(self.loop)
        self.loop.run_until_complete(server.start())
        lost = self.driver.connection_lost

        def connection_lost(addr, _lost=lost, _server=server):
            _server.hap_connection_lost(addr)
            return _lost(addr)
        self.driver.connection_lost = connection_lost
        self.acc = SA.make_accessory_class()(self.driver, self.cam, self.cid, protect, talk, server, chans, sizes, hz,
                                             os.path.join(DATA, "%s.sv.json" % self.cam["mac"]))
        self.driver.add_accessory(self.acc)
        log.info("%s published as a Secure Video camera on port %d (%s); tiers %s; recording sizes %s; sound %s Hz",
                 name, PORT0 + self.index, "paired" if self.driver.state.paired else "waiting to be paired",
                 ", ".join("%dx%d" % (c["width"], c["height"]) for c in chans),
                 ", ".join("%dx%d" % (w, h) for w, h, _f in sizes), hz or "none")
        self.driver.start()
        return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        opts = json.load(open(os.path.join(DATA, "options.json")))
    except Exception:  # noqa: BLE001
        opts = {}
    if (opts.get("log_level") or "") == "debug":
        logging.getLogger().setLevel(logging.DEBUG)
    wanted = [c for c in (opts.get("cameras") or []) if isinstance(c, str)]
    if not wanted:
        log.info("no cameras chosen — choose them in ProOS Pro, Apple Home")
    from proosha.ha_ws import ws_command
    from proosha.ha_stream import HaStream
    ents = ws_command(BASE, _token(), "config/entity_registry/list") or []
    devs = ws_command(BASE, _token(), "config/device_registry/list") or []
    stream = HaStream(BASE, _token())
    stream.start()
    for _ in range(60):
        if stream.healthy():
            break
        time.sleep(0.5)
    states = {s["entity_id"]: s for s in stream.all_states()} if stream.healthy() else {}
    cams = plan(wanted, ents, devs, states)
    secure = {c for c in (opts.get("secure_video") or []) if isinstance(c, str)}      # 970y: Route 2, per camera
    for cam in cams:
        cam["secure_video"] = cam.get("entity") in secure
    published, by_motion, by_ring, by_sensor = [], {}, {}, {}
    for i, cam in enumerate(cams):
        try:
            pcam = protect.camera_for_mac(cam["mac"]) or {}
        except Exception as e:  # noqa: BLE001
            log.error("%s: Protect not reachable: %s", cam["name"], e)
            continue
        cid = pcam.get("id")
        cam["speaker"] = bool((pcam.get("featureFlags") or {}).get("hasSpeaker"))   # Protect's own word
        if not cid:
            log.error("%s: Protect does not list a camera with MAC %s", cam["name"], cam["mac"])
            continue
        p = Published(cam, cid, i)
        p.start()
        published.append(p)
        for t in [cam.get("motion"), cam.get("package")]:
            if t:
                by_motion[t] = p
        for t in cam.get("motion_sensors") or []:
            by_sensor[t] = p
        log.info("%s: motion from %s; ring from %s", cam["name"],
                 ", ".join([t for t in [cam.get("motion"), cam.get("package")] + list(cam.get("motion_sensors") or [])
                            if t]) or "nothing", cam.get("doorbell") or "nothing")
        if cam.get("doorbell"):
            by_ring[cam["doorbell"]] = p

    def on_change(eid):
        ps = by_sensor.get(eid)
        if ps and ps.acc:
            snap = stream.snapshot(ps.cam["motion_sensors"]) or {}
            ps.acc.motion_state(any((snap.get(i) or {}).get("state") == "on" for i in ps.cam["motion_sensors"]))
            return
        p = by_motion.get(eid) or by_ring.get(eid)
        if p and p.acc:
            st = (stream.snapshot([eid]) or {}).get(eid) or {}
            if st.get("state") not in ("unavailable", "unknown", None):
                (p.acc.ring if eid in by_ring else p.acc.motion)()
    stream.on_change = on_change
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    last = {p.cam["mac"]: None for p in published}
    while not stop.is_set():
        stop.wait(10)
        for p in published:      # a pairing made or removed in the Home app updates the notice Pro reads — once
            if p.driver and last.get(p.cam["mac"]) is not p.driver.state.paired:
                last[p.cam["mac"]] = p.driver.state.paired
                notify(p.cam["mac"], p.cam["name"], code_for(p.cam["mac"]), p.driver.state.paired)
    for p in published:
        if p.driver:
            p.loop.call_soon_threadsafe(lambda d=p.driver: asyncio.ensure_future(d.async_stop()))


if __name__ == "__main__":
    main()
