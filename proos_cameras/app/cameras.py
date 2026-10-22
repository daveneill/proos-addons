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
import threading
import time
import urllib.request

import protect

log = logging.getLogger("proos_cameras")

BASE = os.environ.get("PROOS_HA_BASE", "http://supervisor/core")
DATA = os.environ.get("PROOS_DATA", "/data")
PORT0 = 51900                    # first Apple Home port; one per camera, in order (plumbing)
EVENT_PULSE = 1.0                # s an event-type motion is held "detected" for Apple (plumbing)

RESOLUTIONS = [(320, 180), (320, 240), (480, 270), (480, 360), (640, 360), (640, 480), (1024, 576), (1024, 768),
               (1280, 720), (1280, 960), (1920, 1080), (1600, 1200)]      # the platform's own list (type_cameras)


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
        out.append({"entity": eid, "mac": mac, "name": d.get("name_by_user") or d.get("name") or eid,
                    "model": d.get("model") or "UniFi Protect", "sw": d.get("sw_version") or "",
                    "motion": ev("motion"), "doorbell": ev("doorbell")})
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
def ffmpeg_live(src: str, s: dict, audio_port: int | None) -> list:
    """The ffmpeg command for one live view: Protect's stream in, video copied as SRTP to the iPhone, sound as Opus
    to the timing proxy (which encrypts and sends it) — the platform's own command shape (type_cameras VIDEO_OUTPUT /
    AUDIO_OUTPUT)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-rtsp_transport", "tcp", "-i", src,
           "-map", "0:v:0", "-an", "-c:v", "copy", "-payload_type", "99", "-ssrc", str(s["v_ssrc"]), "-f", "rtp",
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
        def __init__(self, cam: dict, cid: str, driver, address: str):
            opts = {
                "video": {"codec": {"profiles": [PF["BASELINE"], PF["MAIN"], PF["HIGH"]],      # the platform's own
                                    "levels": [LV["TYPE3_1"], LV["TYPE3_2"], LV["TYPE4_0"]]},
                          "resolutions": [[w, h, 30] for w, h in RESOLUTIONS]},
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
            if cam.get("motion"):
                self.char_motion = self.add_preload_service("MotionSensor").configure_char(
                    "MotionDetected", value=False)

        # motion — from the platform's own motion event for this camera
        def motion(self):
            if not self.char_motion:
                return
            self.char_motion.set_value(True)
            threading.Timer(EVENT_PULSE, lambda: self.char_motion.set_value(False)).start()

        def get_snapshot(self, image_size):  # noqa: ARG002 — Apple's size is a hint; Protect's own picture is used
            return protect.snapshot(self.cid)

        async def start_stream(self, session_info, stream_config):
            from homekit_audio_proxy import AudioProxy
            try:
                src = protect.stream_url(self.cid, "high")
            except Exception as e:  # noqa: BLE001
                log.error("%s: no stream from Protect: %s", self.cam["name"], e)
                return False
            proxy = AudioProxy(dest_addr=stream_config["address"], dest_port=stream_config["a_port"],
                               srtp_key_b64=stream_config["a_srtp_key"],
                               target_clock_rate=int(stream_config.get("a_sample_rate") or 16) * 1000)
            await proxy.async_start()
            port = proxy.local_port or None
            cmd = ffmpeg_live(src, stream_config, port)
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            session_info["process"], session_info["proxy"] = proc, proxy
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
                await proxy.async_stop()
            log.info("%s: live view stopped", self.cam["name"])

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
        code = code_for(self.cam["mac"])
        self.driver = AccessoryDriver(port=PORT0 + self.index, persist_file=os.path.join(
            DATA, "%s.state" % self.cam["mac"]), pincode=code.encode(), loop=self.loop)
        self.acc = make_accessory_class()(self.cam, self.cid, self.driver, self.driver.state.address)
        self.driver.add_accessory(self.acc)
        log.info("%s published on port %d (%s)", self.cam["name"], PORT0 + self.index,
                 "paired" if self.driver.state.paired else "waiting to be paired")
        self.driver.start()


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
    published, by_motion = [], {}
    for i, cam in enumerate(cams):
        try:
            cid = protect.camera_id_for_mac(cam["mac"])
        except Exception as e:  # noqa: BLE001
            log.error("%s: Protect not reachable: %s", cam["name"], e)
            continue
        if not cid:
            log.error("%s: Protect does not list a camera with MAC %s", cam["name"], cam["mac"])
            continue
        p = Published(cam, cid, i)
        p.start()
        published.append(p)
        if cam.get("motion"):
            by_motion[cam["motion"]] = p

    def on_change(eid):
        p = by_motion.get(eid)
        if p and p.acc:
            st = (stream.snapshot([eid]) or {}).get(eid) or {}
            if st.get("state") not in ("unavailable", "unknown", None):
                p.acc.motion()
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
