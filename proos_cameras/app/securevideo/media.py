"""ProOS Cameras — Secure Video's media: live view tiers, the rolling buffer, and event recordings. Register 970y.

Everything here follows tested code, named where used:
  - live view: one Apple tier = one Protect channel, its own HEVC passed through (no conversion) as SRTP with the
    keys the Home app gave. Front Door measured on the box: every key frame carries VPS/SPS/PPS; and dump_extra puts the
    stream's headers before every key frame anyway — ffmpeg otherwise moves headers announced only once into an SDP
    the Home app never gets (measured in the cloud: nothing decodable without it, 970y). Sound and talk-back go through talk.Relay (the classic camera's, proven on Front Door); Secure Video
    advertises Opus at 48 kHz (Apple's guide), so its clock conversion is a pass-through.
  - recording (homebridge-plugin-utils 2.4.0 record.js / options.js, and homebridge-unifi-protect 8.1.0
    media/record.js): a copy of the camera is kept as ~1 s fMP4 pieces in memory while recording is on; when the hub
    opens a recording, the pieces from a key frame at or before the pre-buffer, then the live ones, are fed to a second
    ffmpeg that makes H.264 at the hub's chosen size / rate / bit rate / profile / level, a key frame every 4 s, no
    B-frames; sound copied; pieces handed to the hub no sooner than 4 s apart (the plugin's pacing).
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import random
import time

from . import fmp4

log = logging.getLogger("proos_cameras.securevideo.media")

H264_PROFILES = ["baseline", "main", "high"]
H264_LEVELS = ["3.1", "3.2", "4.0"]
HKSV_IDR_INTERVAL = 4          # s between key frames in a recording (homebridge-plugin-utils settings.js)
HKSV_PACING = 4.0              # s minimum between pieces handed to the hub (HKSV_TIMEOUT 4500 − 500, the plugin)
PIECE_S = 1.0                  # s per piece in the rolling buffer (the plugin's livestream segment length)
KEEP_EXTRA_S = 6.0             # s kept beyond the pre-buffer, so a key frame before it is always held
RECORD_PROBESIZE = 5000000     # bytes (the plugin's default)
TIER_QUALITY = {"high": 2, "medium": 3, "low": 4}


def srtp_key(params: dict) -> str:
    return base64.b64encode(bytes(params["masterKey"]) + bytes(params["masterSalt"])).decode()


def tiers_from(channels: list) -> list:
    """channels: [{quality, width, height, fps, kbps}] measured on the box → Apple's tiers, ids 1…n in order given.
    Bit rates are the camera's own measured ones (passed through, so they cannot be changed); peak = 110 % (Apple:
    the gap should be 10 % or less)."""
    out = []
    for i, c in enumerate(channels, 1):
        kbps = max(1, int(round(c["kbps"])))
        out.append({"identifier": i, "quality": TIER_QUALITY[c["quality"]], "targetAverageBitrate": kbps,
                    "width": int(c["width"]), "height": int(c["height"]), "frameRate": int(round(c["fps"])),
                    "peakBitrate": int(round(kbps * 1.1))})
    return out


def live_command(src: str, req: dict, sess: dict, relay_port: int | None, audio: dict | None) -> list:
    """One tier, passed through: video copied as SRTP to the Home app; sound as Opus to the relay."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-rtsp_transport", "tcp", "-i", src,
           "-map", "0:v:0", "-an", "-c:v", "copy", "-bsf:v", "dump_extra=freq=keyframe",
           "-payload_type", str(sess["video_pt"]), "-ssrc", str(sess["vssrc"]), "-f", "rtp",
           "-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80", "-srtp_out_params", srtp_key(sess["video"]),
           "srtp://%s:%d?rtcpport=%d&localrtpport=%d&pkt_size=1316"
           % (sess["target"], sess["cvp"], sess["cvp"], sess["cvp"])]
    if relay_port and audio:
        cmd += ["-map", "0:a:0?", "-vn", "-c:a", "libopus", "-application", "lowdelay", "-ac", "1",
                "-ar", "%d" % audio["capture_hz"], "-b:a", "%d" % audio["bps"], "-frame_duration", "20",
                "-payload_type", str(audio["pt"]), "-ssrc", str(sess["assrc"]), "-f", "rtp",
                "rtp://127.0.0.1:%d?pkt_size=188" % relay_port]
    return cmd


class LiveView:
    """The multi-tier live-view delegate (controller.SecureVideoCamera calls it; every call returns at once)."""

    def __init__(self, name: str, loop, channel_url, tiers: list, audio: dict | None, relay_factory=None,
                 video_pt: int = 99):
        """channel_url(quality) → RTSPS address; tiers: the channel list given to tiers_from, same order (tier id 1 =
        the first); audio: {pt, bps, capture_hz}; relay_factory(address, port, key_b64, sid) → started talk.Relay."""
        self.name, self.loop, self.channel_url = name, loop, channel_url
        self.tier_quality = {i: c["quality"] for i, c in enumerate(tiers, 1)}
        self.audio, self.relay_factory, self.video_pt = audio, relay_factory, video_pt
        self.sessions = {}

    def prepare_stream(self, req: dict) -> dict:
        sess = {"target": req["targetAddress"], "cvp": req["controllerVideoPort"], "cap": req["controllerAudioPort"],
                "video": req["video"], "audio": req["audio"], "video_pt": self.video_pt, "proc": None, "relay": None}
        self.sessions[req["sessionIdentifier"]] = sess
        log.info("%s: live view prepared for %s (video %d, sound %d)", self.name, sess["target"], sess["cvp"], sess["cap"])
        # our ports = the Home app's own numbers, as the classic camera answers (HAP-python), so return audio and
        # RTCP arrive where the relay and ffmpeg listen; SRTP keys echoed (we send with the keys we were given)
        # 970z: the stream ids announced HERE are the ones sent with. The author of the tested code (camera.ui
        # HomeKit plugin, multiTierRtpDelegate.ts): "iOS filters the incoming stream by the ssrc from the Setup
        # Endpoints response" — not the one in the START request, which HAP-NodeJS's own comment says to use; 0.3.0
        # used START's, and the iPhone threw the picture away (Front Door, 05:53).
        sess["vssrc"], sess["assrc"] = random.getrandbits(32) or 1, random.getrandbits(32) or 1
        return {"videoPort": sess["cvp"], "audioPort": sess["cap"], "videoSSRC": sess["vssrc"],
                "audioSSRC": sess["assrc"], "video": req["video"], "audio": req["audio"]}

    def start_stream(self, req: dict):
        sess = self.sessions.get(req["sessionIdentifier"])
        if sess is None:
            raise KeyError("unknown live-view session")
        quality = self.tier_quality.get(req["videoTier"])
        if quality is None:
            raise KeyError("unknown tier %r" % req["videoTier"])
        asyncio.run_coroutine_threadsafe(self._run(req, sess, quality), self.loop)

    async def _run(self, req, sess, quality):
        try:
            src = await self.loop.run_in_executor(None, self.channel_url, quality)
        except Exception as e:  # noqa: BLE001
            log.error("%s: no %s stream from Protect: %s", self.name, quality, e)
            return
        relay = None
        if self.relay_factory and self.audio:
            relay = self.relay_factory(sess["target"], sess["cap"], srtp_key(sess["audio"]), req["sessionIdentifier"])
        sess["relay"] = relay
        cmd = live_command(src, req, sess, relay.local_port if relay else None, self.audio)
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        sess["proc"] = proc
        log.info("%s: live view started — %s tier passed through to %s", self.name, quality, sess["target"])

        async def errors():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                log.warning("%s: ffmpeg: %s", self.name, line.decode(errors="replace").rstrip()[:300])
            log.info("%s: live view ffmpeg ended (exit %s)", self.name, await proc.wait())
        asyncio.ensure_future(errors())

    def stop_stream(self, sid: str):
        sess = self.sessions.pop(sid, None)
        if not sess:
            return
        proc, relay = sess.get("proc"), sess.get("relay")
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        if relay:
            relay.stop()
            log.info("%s: live view stopped (sound out %d packets, voice to the door %d)", self.name,
                     relay.packets_out, relay.packets_back)


class Timeshift:
    """A copy of the camera kept as ~1 s fMP4 pieces in memory (the plugin's timeshift buffer)."""

    def __init__(self, name: str, loop, channel_url, quality: str = "medium", keep_s: float = 10.0):
        self.name, self.loop, self.channel_url, self.quality = name, loop, channel_url, quality
        self.keep_s = keep_s
        self.init = None
        self.ring = collections.deque()          # (monotonic seconds, bytes, starts_with_key_frame)
        self.listeners = []
        self.proc = None
        self.task = None
        self.running = False

    def command(self, src: str) -> list:
        return ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-fflags", "+discardcorrupt",
                "-err_detect", "ignore_err", "-max_delay", "500000", "-avioflags", "direct", "-rtsp_transport", "tcp",
                "-i", src, "-map", "0:v:0", "-codec:v", "copy", "-frag_duration", str(int(PIECE_S * 1000000)),
                "-movflags", "default_base_moof+empty_moov+frag_keyframe+skip_sidx+skip_trailer",
                "-flush_packets", "1", "-reset_timestamps", "1", "-map", "0:a:0?", "-codec:a", "copy",
                "-f", "mp4", "pipe:1"]

    def start(self):
        if not self.running:
            self.running = True
            self.task = asyncio.ensure_future(self._run(), loop=self.loop)

    def stop(self):
        self.running = False
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
        self.ring.clear()
        self.init = None

    def held_seconds(self) -> float:
        return (self.ring[-1][0] - self.ring[0][0] + PIECE_S) if self.ring else 0.0

    async def _run(self):
        while self.running:
            try:
                src = await self.loop.run_in_executor(None, self.channel_url, self.quality)
                self.proc = await asyncio.create_subprocess_exec(*self.command(src), stdin=asyncio.subprocess.DEVNULL,
                                                                 stdout=asyncio.subprocess.PIPE,
                                                                 stderr=asyncio.subprocess.DEVNULL)
                log.info("%s: recording buffer started (%s stream copied)", self.name, self.quality)
                cut = fmp4.Cutter()
                while True:
                    data = await self.proc.stdout.read(65536)
                    if not data:
                        break
                    for piece in cut.feed(data):
                        if piece[0] == "init":
                            self.init = piece[1]
                            continue
                        now = time.monotonic()
                        self.ring.append((now, piece[1], piece[2]))
                        while self.ring and now - self.ring[0][0] > self.keep_s:
                            self.ring.popleft()
                        for fn in list(self.listeners):
                            fn(piece[1])
                rc = await self.proc.wait()
                if self.running:
                    log.warning("%s: recording buffer ended (exit %s) — restarting in 5 s", self.name, rc)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: recording buffer failed: %s — retrying in 5 s", self.name, e)
            self.ring.clear()
            self.init = None
            if self.running:
                await asyncio.sleep(5)

    def aligned_start(self, prebuffer_s: float):
        """(index, seek seconds): the last piece starting on a key frame at or before `prebuffer_s` ago, and how far
        into it the pre-buffer begins (the plugin's getKeyframeAlignedStart / -ss)."""
        if not self.ring:
            return None
        target = time.monotonic() - prebuffer_s
        best = None
        for i, (t, _b, key) in enumerate(self.ring):
            if key and t <= target:
                best = i
        if best is None:
            for i, (_t, _b, key) in enumerate(self.ring):
                if key:
                    best = i
                    break
        if best is None:
            return None
        return best, max(0.0, target - self.ring[best][0])


def record_command(cfg: dict, seek_s: float, name: str, video_filters=None) -> list:
    """The plugin's recording command: fMP4 in on stdin, H.264 fMP4 out at the hub's choice, sound copied."""
    v = cfg["video"]
    fps, h, br = int(v["fps"]), int(v["height"]), int(v["bitrate"])
    prof = H264_PROFILES[v["profile"]] if 0 <= v["profile"] < 3 else "high"
    lev = H264_LEVELS[v["level"]] if 0 <= v["level"] < 3 else "4.0"
    filters = ["scale=-2:min(ih\\, %d):in_range=auto:out_range=auto" % h, "fps=%d" % fps] + list(video_filters or [])
    return ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-fflags", "+discardcorrupt",
            "-err_detect", "ignore_err", "-max_delay", "500000",
            "-flags", "low_delay", "-probesize", str(RECORD_PROBESIZE), "-f", "mp4", "-i", "pipe:0",
            "-ss", "%dms" % int(seek_s * 1000),
            "-map", "0:v:0", "-codec:v", "libx264", "-preset", "veryfast", "-profile:v", prof, "-level:v", lev,
            "-noautoscale", "-bf", "0", "-filter:v", ", ".join(filters), "-g:v", str(fps * HKSV_IDR_INTERVAL),
            "-bufsize", "%dk" % (2 * br), "-maxrate", "%dk" % br, "-b:v", "%dk" % br,
            "-movflags", "default_base_moof+empty_moov+frag_keyframe+skip_sidx+skip_trailer", "-flush_packets", "1",
            "-reset_timestamps", "1", "-metadata", "comment=%s HKSV Event" % name,
            "-map", "0:a:0?", "-codec:a", "copy", "-f", "mp4", "pipe:1"]


class Recorder:
    """The recording delegate (recording.RecordingManagement calls it)."""

    def __init__(self, name: str, timeshift: Timeshift, pacing: float = HKSV_PACING):
        self.name, self.ts, self.pacing = name, timeshift, pacing
        self.cfg = None
        self.active = False

    def _sync(self):
        if self.active and self.cfg:
            self.ts.keep_s = max(self.ts.keep_s, self.cfg["prebuffer_ms"] / 1000.0 + KEEP_EXTRA_S)
            self.ts.start()
        else:
            self.ts.stop()

    def update_recording_configuration(self, cfg):
        self.cfg = cfg
        self._sync()

    def update_recording_active(self, active: bool):
        self.active = bool(active)
        log.info("%s: recording to iCloud %s in the Home app", self.name, "switched on" if self.active else "switched off")
        self._sync()

    def close_recording_stream(self, stream_id, reason):
        log.info("%s: recording %s closed (reason %s)", self.name, stream_id, reason)

    def acknowledge_stream(self, stream_id):
        log.info("%s: recording %s acknowledged by the hub", self.name, stream_id)

    async def stream(self, stream_id, stop: asyncio.Event):
        if not self.cfg or self.ts.init is None:
            raise RuntimeError("no recording buffer yet")
        prebuffer = self.cfg["prebuffer_ms"] / 1000.0
        start = self.ts.aligned_start(prebuffer)
        if start is None:
            raise RuntimeError("no key frame in the recording buffer")
        index, seek = start
        cmd = record_command(self.cfg, seek, self.name)
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        log.info("%s: recording %s — %d s held, starting %.1f s before the event, %sx%s at %s kbit/s", self.name,
                 stream_id, int(self.ts.held_seconds()), prebuffer, "auto", self.cfg["video"]["height"],
                 self.cfg["video"]["bitrate"])
        backlog = [self.ts.init] + [b for (_t, b, _k) in list(self.ts.ring)[index:]]
        live = asyncio.Queue()
        listener = live.put_nowait
        self.ts.listeners.append(listener)

        async def feed():
            try:
                for b in backlog:
                    proc.stdin.write(b)
                    await proc.stdin.drain()
                while not stop.is_set():
                    b = await live.get()
                    proc.stdin.write(b)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
                pass
        feeder = asyncio.ensure_future(feed())
        cut = fmp4.Cutter()
        last = time.monotonic()
        sent = 0
        try:
            while not stop.is_set():
                try:
                    data = await asyncio.wait_for(proc.stdout.read(65536), 1.0)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    break
                for piece in cut.feed(data):
                    wait = last + self.pacing - time.monotonic()
                    if wait > 0:
                        try:
                            await asyncio.wait_for(stop.wait(), wait)
                        except asyncio.TimeoutError:
                            pass
                    if stop.is_set():
                        break
                    yield piece[1], False
                    last = time.monotonic()
                    sent += 1
        finally:
            if listener in self.ts.listeners:
                self.ts.listeners.remove(listener)
            feeder.cancel()
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            log.info("%s: recording %s ended — %d pieces sent", self.name, stream_id, sent)
