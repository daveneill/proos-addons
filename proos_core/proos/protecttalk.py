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
"""
from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import threading
import time

IDLE = 4.0              # s without audio before a session is closed (plumbing)
MAX_SESSIONS = 4        # talk sessions at once on one box (plumbing)
_CODECS = {"opus": ["-c:a", "libopus", "-b:a", "48k", "-application", "voip", "-frame_duration", "20"],
           "aac": ["-c:a", "aac", "-b:a", "64k"]}
_URL = re.compile(r"^rtp://[0-9A-Za-z.\-\[\]:]+:\d{2,5}$")

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
    proc = popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sid = secrets.token_hex(8)
    with _lock:
        _sessions[sid] = {"proc": proc, "cam": camera_id, "rate": int(rate), "last": time.time()}
    _ensure_reaper()
    return {"session": sid, "rate": int(rate), "codec": codec, "idle": IDLE}


def audio(sid: str, pcm: bytes) -> None:
    with _lock:
        s = _sessions.get(sid)
        if not s:
            raise TalkError(404, "that talk session has ended")
        s["last"] = time.time()
        proc = s["proc"]
    if not pcm:
        return
    try:
        proc.stdin.write(pcm)
        proc.stdin.flush()
    except Exception:  # noqa: BLE001 — the encoder went away: the session is over
        stop(sid)
        raise TalkError(410, "the camera stopped listening")


def stop(sid: str) -> bool:
    with _lock:
        s = _sessions.pop(sid, None)
    if not s:
        return False
    _close(s["proc"])
    return True


def _close(proc) -> None:
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
        threading.Thread(target=_close, args=(_sessions.pop(sid)["proc"],), daemon=True).start()


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
