"""ProOS Core — two-way talk to a UniFi Protect camera, through Ubiquiti's official API.

REGISTER 700 (Dave, 7 Oct 2026: "we need to support the talk back and two-way audio" … "2. B"
— build ProOS's own push-to-talk now — "Everything needs to support Unifi API not reverse
engineered").

The Talk button before this sent nothing: it opened a talk session and said "Talkback session
started", and no sound ever left the phone. Now:

  1. start(camera): Core asks the camera for a talk session through the OFFICIAL Protect
     Integration API — POST /v1/cameras/{id}/talkback-session. Read on Dave's G6 Pro Entry,
     7 Oct 2026: {"url": "rtp://192.168.8.176:7004", "codec": "opus", "samplingRate": 24000,
     "bitsPerSample": 16}.
  2. audio(session, pcm): the page sends the microphone as 16-bit mono PCM at the camera's
     own rate, a slice at a time. Core feeds it to ffmpeg, which encodes it in the camera's
     codec and sends it as RTP to the address the camera gave.
  3. stop(session), or IDLE seconds with no audio: ffmpeg is closed; the camera's session
     ends with the stream.

Hearing the camera needs nothing here: the platform's live view already carries its
microphone; the page un-mutes it.

Nothing is recorded or kept: audio passes from the request straight into ffmpeg's input.

REGISTER 702 — A RECORD OF EVERY TALK SESSION (Dave, 7 Oct 2026: voice "not reaching the door" sometimes).
The audio is never kept, but what HAPPENED is: the last LOG_KEEP sessions, each with when it opened, the
camera's address and codec, how many slices and bytes arrived, the longest gap between slices, how it ended
(stopped / went quiet / encoder gone), the encoder's exit code and its last words. ffmpeg's error output was
thrown away before, so a session that failed said nothing at all. log() serves the record (GET
/protect/talk/log); it holds no audio and no names of people.

REGISTER 707 — HOW LOUD IT WAS. Dave, 14:22 the same day, on the 846 app: "Still no audio on push to talk at front
door". The record proved the voice ARRIVED on time (e.g. 3.9 s open, 167 KB = 3.5 s at 24 kHz) and the encoder sent
it without complaint — the same path a test tone took to the door that morning. What it could not say is whether
what arrived was sound. Each session now carries the loudest sample (peak, of 32767), the overall level (rms), and
how many slices were silent (peak under SILENT_PEAK). Numbers only — never the audio.
"""
from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import threading
import sys
import time
from array import array
from collections import deque

IDLE = 4.0              # s without audio before a session is closed (plumbing)
MAX_SESSIONS = 4        # talk sessions at once on one box (plumbing)
_CODECS = {"opus": ["-c:a", "libopus", "-b:a", "48k", "-application", "voip", "-frame_duration", "20"],
           "aac": ["-c:a", "aac", "-b:a", "64k"]}
_URL = re.compile(r"^rtp://[0-9A-Za-z.\-\[\]:]+:\d{2,5}$")

LOG_KEEP = 20           # sessions kept in the record (plumbing)
SILENT_PEAK = 64        # a slice whose loudest sample is under this (of 32767, about -54 dBFS) counts as silent (plumbing: a reading's label, never acted on)
_log: deque = deque(maxlen=LOG_KEEP)
_lock = threading.Lock()
_sessions: dict = {}     # id -> {"proc", "cam", "rate", "last"}
_reaper = {"t": None}


class TalkError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def available() -> bool:
    return bool(shutil.which("ffmpeg"))


def ffmpeg_args(url: str, codec: str, rate: int) -> list:
    """The ffmpeg command for one talk session: PCM in, the camera's codec out, RTP to its URL."""
    if not _URL.match(str(url or "")):
        raise TalkError(502, "the camera gave a talk address ProOS does not recognise")
    enc = _CODECS.get(str(codec or "").lower())
    if not enc:
        raise TalkError(502, "the camera asked for %s, which ProOS cannot send yet" % codec)
    rate = int(rate or 24000)
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
            "-ar", str(rate), "-ac", "1"] + enc + ["-f", "rtp", url]


def start(client, camera_id: str, popen=subprocess.Popen) -> dict:
    """Open a talk session on the camera (official API) and the encoder for it."""
    if not re.match(r"^[0-9a-f]{24}$", str(camera_id or "")):
        raise TalkError(404, "not a camera")
    if not available() and popen is subprocess.Popen:
        raise TalkError(503, "talk needs ffmpeg in ProOS Core, and this box does not have it yet")
    with _lock:
        _reap_locked(time.time())
        if len(_sessions) >= MAX_SESSIONS:
            raise TalkError(429, "too many people are talking through cameras at once")
    info = client.talkback(camera_id) or {}
    url, codec, rate = info.get("url"), info.get("codec"), info.get("samplingRate") or 24000
    args = ffmpeg_args(url, codec, rate)
    proc = popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    sid = secrets.token_hex(8)
    now = time.time()
    rec = {"session": sid, "camera": camera_id, "address": url, "codec": codec, "rate": int(rate),
           "opened": now, "slices": 0, "bytes": 0, "longest_gap_s": 0.0, "first_audio_s": None,
           "ended": None, "how": None, "encoder_exit": None, "encoder_said": "",
           "peak": 0, "rms": 0.0, "silent_slices": 0, "_sumsq": 0.0, "_n": 0}
    _watch_stderr(proc, rec)
    with _lock:
        _sessions[sid] = {"proc": proc, "cam": camera_id, "rate": int(rate), "last": now, "rec": rec}
        _log.append(rec)
    _ensure_reaper()
    return {"session": sid, "rate": int(rate), "codec": codec, "idle": IDLE}


def audio(sid: str, pcm: bytes) -> None:
    with _lock:
        s = _sessions.get(sid)
        if not s:
            raise TalkError(404, "that talk session has ended")
        now = time.time()
        rec = s.get("rec")
        if rec is not None and pcm:
            if rec["slices"]:
                rec["longest_gap_s"] = round(max(rec["longest_gap_s"], now - s["last"]), 3)
            else:
                rec["first_audio_s"] = round(now - rec["opened"], 3)
            rec["slices"] += 1
            rec["bytes"] += len(pcm)
            _measure(rec, pcm)
        s["last"] = now
        proc = s["proc"]
    if not pcm:
        return
    try:
        proc.stdin.write(pcm)
        proc.stdin.flush()
    except Exception:  # noqa: BLE001 — the encoder went away: the session is over
        stop(sid, how="encoder gone")
        raise TalkError(410, "the camera stopped listening")


def stop(sid: str, how: str = "stopped") -> bool:
    with _lock:
        s = _sessions.pop(sid, None)
    if not s:
        return False
    _close(s["proc"], s.get("rec"), how)
    return True


def _close(proc, rec=None, how: str = "stopped") -> None:
    try:
        _close_proc(proc)
    finally:
        if rec is not None:
            rec["ended"] = time.time()
            rec["how"] = how
            try:
                rec["encoder_exit"] = proc.poll() if hasattr(proc, "poll") else None
            except Exception:  # noqa: BLE001
                pass


def _watch_stderr(proc, rec) -> None:
    """Keep the encoder's last words (its error output) on the session's record."""
    err = getattr(proc, "stderr", None)
    if err is None or not hasattr(err, "readline"):
        return

    def run():
        try:
            for line in iter(err.readline, b""):
                rec["encoder_said"] = (rec["encoder_said"] + line.decode("utf-8", "replace"))[-400:]
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=run, name="protect-talk-err", daemon=True).start()


def _measure(rec: dict, pcm: bytes) -> None:
    """Loudness of one slice of 16-bit little-endian mono PCM, folded into the session's record."""
    try:
        a = array("h")
        a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if sys.byteorder != "little":
            a.byteswap()
        if not a:
            return
        pk = max(max(a), -min(a))
        rec["peak"] = max(rec["peak"], min(pk, 32767))
        rec["_sumsq"] += float(sum(x * x for x in a))
        rec["_n"] += len(a)
        rec["rms"] = round((rec["_sumsq"] / rec["_n"]) ** 0.5, 1)
        if pk < SILENT_PEAK:
            rec["silent_slices"] += 1
    except Exception:  # noqa: BLE001 — a reading, never in the way of the voice
        pass


def log() -> list:
    """The record of the last sessions, newest first. No audio, ever."""
    with _lock:
        out = [{k: v for k, v in r.items() if not k.startswith("_")} for r in reversed(_log)]
    for r in out:
        r["open_s"] = round((r["ended"] or time.time()) - r["opened"], 1)
    return out


def _close_proc(proc) -> None:
    try:
        proc.stdin.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _reap_locked(now: float) -> None:
    for sid in [k for k, v in _sessions.items() if now - v["last"] > IDLE]:
        s = _sessions.pop(sid)
        threading.Thread(target=_close, args=(s["proc"], s.get("rec"), "went quiet"), daemon=True).start()


def reap(now: float | None = None) -> int:
    with _lock:
        before = len(_sessions)
        _reap_locked(time.time() if now is None else now)
        return before - len(_sessions)


def _ensure_reaper() -> None:
    with _lock:
        t = _reaper["t"]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_reap_loop, name="protect-talk", daemon=True)
        _reaper["t"] = t
    t.start()


def _reap_loop() -> None:
    while True:
        time.sleep(1.0)
        reap()
        with _lock:
            if not _sessions:
                _reaper["t"] = None
                return


def sessions() -> int:
    with _lock:
        return len(_sessions)
