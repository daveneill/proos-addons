"""ProOS Cameras — one UniFi camera as Apple's new Secure Video camera (Route 2). Register 970y.

Puts controller.SecureVideoCamera on a HAP-python accessory, with Protect behind it: live-view tiers from the camera's
own channels (measured at start-up), recordings from media.Recorder, pictures from Protect, the doorbell's ring and
two-way talk as the classic camera does them (970t). A camera in this mode is a NEW accessory to Apple Home (its own
state file) — it is added again in the Home app, which is what lets Apple offer People / Vehicles / Animals / Packages.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time

from . import controller as SV
from . import datastream as D
from . import media as M
from . import recording as R

log = logging.getLogger("proos_cameras.securevideo")

AUDIO = {"pt": 110, "bps": 24000, "capture_hz": 24000}                       # Apple: Opus, 24 kHz capture recommended
AUDIO_TIER = {"identifier": 1, "targetAverageBitrate": 24000, "sampleRate": 4, "bitDepth": 2, "packetTime": 20,
              "channels": 1}                                                  # bits/s; 48 kHz on the wire (Apple's guide)
EVENT_PULSE = 1.0
MEASURE_RETRIES = 2          # 970hh: plumbing — how many more tries a silent Protect channel gets at start-up
MEASURE_GAP_S = 2.0          # 970hh: plumbing — the pause before each retry


def measure_channel(src: str, seconds: float = 4.0, run=None, why: list | None = None) -> dict:
    """A Protect channel as it really is: codec, size, frame rate (ffprobe) and bit rate (bytes copied for `seconds`).
    {} when it cannot be read, with the reason appended to `why` (970hh). Front Door measured this way on the box,
    22 Oct (970y)."""
    run = run or subprocess.run
    why = why if why is not None else []
    try:
        import json
        p = run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0", "-show_entries",
                 "stream=codec_name,width,height,avg_frame_rate", "-of", "json", src],
                capture_output=True, text=True, timeout=15)
        st = (json.loads(p.stdout or "{}").get("streams") or [{}])[0]
        if not st.get("width"):
            why.append("the stream's details did not come back (%s)"
                       % ((getattr(p, "stderr", "") or "").strip()[-160:] or "exit %s" % getattr(p, "returncode", "?")))
            return {}
        num, _, den = str(st.get("avg_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        c = run(["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", src, "-map", "0:v:0", "-c", "copy",
                 "-t", str(seconds), "-f", "data", "pipe:1"], capture_output=True, timeout=seconds + 20)
        kbps = len(c.stdout or b"") * 8 / seconds / 1000.0
        if not kbps:
            err = getattr(c, "stderr", b"") or b""
            err = err.decode(errors="replace") if isinstance(err, bytes) else str(err)
            why.append("no video arrived in %g s (%s)" % (seconds, err.strip()[-160:] or "exit %s"
                                                             % getattr(c, "returncode", "?")))
            return {}
        return {"codec": str(st.get("codec_name") or "").lower(), "width": int(st["width"]), "height": int(st["height"]),
                "fps": round(fps, 2), "kbps": round(kbps)}
    except subprocess.TimeoutExpired as e:
        why.append("no answer within %d s" % int(e.timeout))
        return {}
    except Exception as e:  # noqa: BLE001
        why.append("%s: %s" % (type(e).__name__, e))
        return {}


def recording_options(sizes: list, audio_hz: int) -> dict:
    """What we offer the hub for recordings — the Homebridge UniFi Protect plugin's offer (media/stream.js): H.264 Main,
    levels 3.1 / 3.2 / 4.0, fMP4 in 4 s pieces (shorter switches Secure Video off), 4 s pre-buffer (the most HomeKit
    asks for), the camera's sizes, AAC-LC at the camera's own rate (16 or 48 kHz) so its sound is copied."""
    return {"prebuffer_ms": 4000, "containers": [(R.FRAGMENTED_MP4, 4000)],
            "video": {"codec": R.H264, "profiles": [R.MAIN], "levels": [R.LEVEL3_1, R.LEVEL3_2, R.LEVEL4_0],
                      "resolutions": [tuple(s) for s in sizes]},
            "audio": [{"type": R.AAC_LC, "channels": 1, "samplerates": [R.KHZ_48 if audio_hz == 48000 else R.KHZ_16]}]}


def make_accessory_class():
    from pyhap.accessory import Accessory
    from pyhap.const import CATEGORY_CAMERA, CATEGORY_VIDEO_DOOR_BELL

    class SecureVideoAccessory(Accessory):
        def __init__(self, driver, cam: dict, cid: str, protect, talk, server: D.Server, channels: list,
                     sizes: list, audio_hz: int, state_path: str):
            super().__init__(driver, cam["name"])
            self.cam, self.cid, self.protect, self.talk = cam, cid, protect, talk
            self.category = CATEGORY_VIDEO_DOOR_BELL if cam.get("doorbell") else CATEGORY_CAMERA
            info = self.get_service("AccessoryInformation")
            info.configure_char("Manufacturer", value="Ubiquiti")
            info.configure_char("Model", value=str(cam.get("model") or "UniFi Protect")[:64])
            info.configure_char("SerialNumber", value=cam["mac"])
            if cam.get("sw"):
                info.configure_char("FirmwareRevision", value=str(cam["sw"])[:64])
            self.talking = {}
            self.held = False
            loop = driver.loop
            url = lambda q: protect.stream_url(cid, q)      # noqa: E731
            rq = "medium" if any(c["quality"] == "medium" for c in channels) else channels[0]["quality"]
            hevc = next((c for c in channels if c["quality"] == rq), channels[0]).get("codec") == "hevc"
            # 970cc: an HEVC camera's recordings are its own HEVC passed through in ~4 s pieces (the hub's fragment
            # length) as hvc1, with re-based times and a prft clock each — the camera.ui HomeKit plugin's way
            self.timeshift = M.Timeshift(cam["name"], loop, url, quality=rq, piece_s=4.0 if hevc else M.PIECE_S,
                                         hevc_tag=hevc)
            self.recorder = M.Recorder(cam["name"], self.timeshift, passthrough=hevc)
            self.live = M.LiveView(cam["name"], loop, url, channels, AUDIO, relay_factory=self._relay)
            top = max(channels, key=lambda c: c["width"] * c["height"])
            options = {
                "sensor": {"uuid": SV.hap_uuid_generate("proos-cameras:%s:sensor" % cam["mac"]),
                           "width": top["width"], "height": top["height"]},
                "video": {"codec": 2 if top.get("codec") == "hevc" else 1, "payload_type": 99,
                          "tiers": M.tiers_from(channels)},
                # 970gg: the Speaker service on every camera, as the camera.ui HomeKit plugin does (twoWayAudio: true
                # whatever the camera) — Backyard (no speaker) paired but the Home app said "No Response" and never
                # asked it for a picture; the Speaker service was the one structural difference from Front Door.
                # Voice still only reaches a camera that can play it (can_talk, in _relay).
                "audio": {"payload_type": AUDIO["pt"], "tier": AUDIO_TIER, "two_way": True},
                "recording": recording_options(sizes, audio_hz),
                "event_triggers": R.DOORBELL if cam.get("doorbell") else 0,
            }
            self.sv = SV.SecureVideoCamera(self, options, server, self.live, self.recorder,
                                           snapshot=self._snapshot_async, state_path=state_path,
                                           local_address=self._local_address)
            self.sv.load()
            self.recorder.motion_on = lambda: bool(SV.H.char(self.sv.motion, "MotionDetected").value)   # 970dd
            self.char_ring = self.char_ring_switch = None
            if cam.get("doorbell"):          # the platform's doorbell services, as the classic camera (970t)
                serv = self.add_preload_service("Doorbell")
                self.set_primary_service(serv)
                self.char_ring = serv.configure_char("ProgrammableSwitchEvent", value=0)
                self.char_ring_switch = self.add_preload_service("StatelessProgrammableSwitch").configure_char(
                    "ProgrammableSwitchEvent", value=0, valid_values={"SinglePress": 0})

        # where the Home app reached us — our own address on that very connection
        def _local_address(self, client_addr, version):
            conn = self.driver.http_server.connections.get(client_addr)
            return conn.transport.get_extra_info("sockname")[0]

        def _relay(self, address, port, key_b64, sid):
            can_talk = bool(self.char_ring or self.cam.get("speaker"))
            r = self.talk.Relay(address, port, key_b64, 48, on_voice=(lambda: self._open_talk(sid)) if can_talk else None)
            r.start()
            return r

        def _open_talk(self, sid):
            """The iPhone's first word: Protect's talk session, then ffmpeg to the door (as the classic camera)."""
            sess = self.protect.talkback(self.cid)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            fd, sdp = tempfile.mkstemp(suffix=".sdp")
            with os.fdopen(fd, "w") as fh:
                fh.write(self.talk.talk_sdp(port))
            proc = subprocess.Popen(self.talk.ffmpeg_talk(sdp, sess), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.talking[sid] = (proc, sdp)
            log.info("%s: talking — the iPhone's voice to the door (%s, %s Hz)", self.cam["name"],
                     sess.get("codec"), sess.get("samplingRate"))
            return port

        async def _snapshot_async(self):
            return await asyncio.get_running_loop().run_in_executor(None, self.protect.snapshot, self.cid)

        def get_snapshot(self, image_size):  # noqa: ARG002 — the Home app's picture request (camera tile)
            if not self.sv.snapshot_allowed():
                raise RuntimeError("snapshots are off for this camera in the Home app")
            return self.protect.snapshot(self.cid)

        def ring(self):
            if self.char_ring:
                self.char_ring.set_value(0)
                self.char_ring_switch.set_value(0)
                log.info("%s: ring → Apple Home", self.cam["name"])

        def motion(self):
            loop = self.driver.loop
            loop.call_soon_threadsafe(self.sv.set_motion, True)
            # a pulse never ends motion that Protect still holds on (970aa)
            threading.Timer(EVENT_PULSE, lambda: None if self.held
                            else loop.call_soon_threadsafe(self.sv.set_motion, False)).start()
            log.info("%s: motion → Apple Home (Secure Video)", self.cam["name"])

        def motion_state(self, on: bool):
            """970aa: motion held on while any of Protect's detections is on — the hub records for that long."""
            if bool(on) == self.held:
                return
            self.held = bool(on)
            self.driver.loop.call_soon_threadsafe(self.sv.set_motion, bool(on))
            log.info("%s: motion %s → Apple Home (Secure Video)", self.cam["name"], "started" if on else "ended")

        async def stop(self):
            self.timeshift.stop()
            for sid in list(self.live.sessions):
                self.live.stop_stream(sid)
            for proc, sdp in self.talking.values():
                proc.terminate()
                try:
                    os.unlink(sdp)
                except OSError:
                    pass
            await super().stop()

    return SecureVideoAccessory


def audio_rate(src: str, run=None) -> int:
    """The camera's own sound sample rate (Hz), 0 when it has none or cannot be read."""
    run = run or subprocess.run
    try:
        p = run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "a:0", "-show_entries",
                 "stream=sample_rate", "-of", "default=nw=1:nk=1", src], capture_output=True, text=True, timeout=15)
        return int((p.stdout or "0").strip().splitlines()[0] or 0) if p.stdout.strip() else 0
    except Exception:  # noqa: BLE001
        return 0


def channels_for(protect, cid: str, name: str, retries: int = MEASURE_RETRIES, gap_s: float = MEASURE_GAP_S,
                 measure=None) -> list:
    """Measure the camera's Protect channels in parallel (high, medium, low), largest first; [] if none read.
    970hh: a channel that does not answer is logged with its reason and measured again (one at a time, after a short
    gap) before the camera goes live — Backyard went live twice without its high channel and nothing said why."""
    measure = measure or measure_channel
    out = {}

    def one(q):
        why = []
        try:
            m = measure(protect.stream_url(cid, q), why=why)
        except Exception as e:  # noqa: BLE001
            m, why = {}, ["%s: %s" % (type(e).__name__, e)]
        if m:
            m["quality"] = q
            out[q] = m
        return why
    t0 = time.time()
    reasons = {}
    ts = [threading.Thread(target=lambda q=q: reasons.__setitem__(q, one(q))) for q in ("high", "medium", "low")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    for attempt in range(1, retries + 1):
        missing = [q for q in ("high", "medium", "low") if q not in out]
        if not missing:
            break
        for q in missing:
            log.warning("%s: %s channel not read — %s — trying again (%d of %d)", name, q,
                        "; ".join(reasons.get(q) or ["no reason given"]), attempt, retries)
            time.sleep(gap_s)
            reasons[q] = one(q)
    for q in ("high", "medium", "low"):
        if q not in out:
            log.warning("%s: %s channel could not be read — %s — going live without it", name, q,
                        "; ".join(reasons.get(q) or ["no reason given"]))
    chans = [out[q] for q in ("high", "medium", "low") if q in out]
    for c in chans:
        log.info("%s: %s channel %s %dx%d at %s fps, %d kbit/s", name, c["quality"], c["codec"], c["width"],
                 c["height"], c["fps"], c["kbps"])
    log.info("%s: channels measured in %.1f s", name, time.time() - t0)
    return chans
