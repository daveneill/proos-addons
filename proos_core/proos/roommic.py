"""ROOM MICS — where a room mic's answer is heard, and its mic opening again after a question (register 970vv).

Dave, 24 Oct 2026, 09:10: "I would want the choice of either as an option in the room or devices we build might be a
mic only … they will never sound as good as coming through the room speakers". Each room mic (the platform's
assist_satellite) has one setting, REPLIES THROUGH:

  room    Room Speakers — the answer plays through the room's committed speaker (the default; a mic-only unit)
  device  This Device   — the unit says the answer itself (a unit with its own speaker)

The choice is stored on the ROOM's record (project areas, key "mic_replies" = {satellite entity: "device"}), beside
the room's existing voice-replies switch (assist_voice) — one place for a room's voice settings. Absent = room.

A QUESTION REOPENS THE MIC. Read on Dave's Office Voice board, 24 Oct 09:04: ProOS asked "Did you mean Netflix, or
something else?" with continue_conversation TRUE, and the board went idle. The board's own software (ESPHome
voice_assistant.cpp, read: RUN_END with no TTS start → IDLE) opens its mic again only after IT has played a reply —
and with Room Speakers it was handed nothing to play. The platform's own mechanism for "say something, then listen"
is assist_satellite.start_conversation (read 24 Oct: Office Voice reports it, supported_features 3; called by hand
with a short message, the board went responding → listening in 2.1 s). So, after a question spoken through the room
speaker, the door holds the mic's turn for as long as the question takes to say, and when that turn ends ProOS — only
if the mic is idle (a mic that reopened by itself, like the G-Flip panel's, is left alone) — starts a conversation on
it with a moment of silence, so nothing is said twice.

HOW LONG THE QUESTION TAKES (970ww, Dave 09:40: "i tend to answer quicker than when it turns mic on"). 1.0.1090
watched the room speaker for it; read on the box, the Office speaker never reports an announcement ("speaker_played":
false, both turns) — and with music on it reports "playing" throughout, so that watch would have waited out the music.
The door's fallback, 1.5 s + 0.4 s a word, held "Do you mean music, or something to watch?" for 4.7 s. Now: ProOS
made the question's audio, so it MEASURES it — the same file the speaker was given (the platform's own cache, same
engine, voice, language and words) — and the hold is the speaker starting up plus that length.

TIMINGS below are plumbing.
"""
from __future__ import annotations

import io
import os
import struct
import time
import wave

REPLIES = ("room", "device")
SILENCE_NAME = "proos_silence.wav"
SILENCE_URL = "/local/" + SILENCE_NAME

SPEAKER_START_S = 1.0   # a room speaker fetching and starting an announcement (not reported by the speaker; plumbing)
SAY_CAP_S = 15.0        # never hold a mic's turn longer than this (the door's own ceiling, register 936)
SAT_BUSY_MAX_S = 25.0   # the mic finishing its own turn (the door holds the turn while the question plays)
POLL_S = 0.2
SETTLE_S = 0.2          # after the mic goes idle, a moment for one that reopens by itself to do so

_BUSY = ("processing", "responding")


def replies_for(rec, entity) -> str:
    """The room's choice for this mic. Anything unset or unknown is Room Speakers."""
    v = ((rec or {}).get("mic_replies") or {}).get(entity) if entity else None
    return v if v in REPLIES else "room"


def set_replies(rec: dict, entity: str, choice: str) -> dict:
    """Write the choice onto the room record. Room Speakers is the default, so it is stored as absent."""
    if choice not in REPLIES:
        raise ValueError("replies must be one of %s" % (REPLIES,))
    if not (isinstance(entity, str) and entity.startswith("assist_satellite.")):
        raise ValueError("not a room mic")
    m = dict(rec.get("mic_replies") or {})
    if choice == "room":
        m.pop(entity, None)
    else:
        m[entity] = choice
    if m:
        rec["mic_replies"] = m
    else:
        rec.pop("mic_replies", None)
    return rec


def asks(reply) -> bool:
    """A reply that ends in a question (the same rule as the door's, register 936)."""
    return str(reply or "").rstrip().endswith("?")


def mics_in_room(area_id, entities, devices) -> list:
    """The room mics in a room: assist_satellite entities whose own area, or their device's area, is this room."""
    dev_area = {d.get("id"): d.get("area_id") for d in (devices or []) if isinstance(d, dict)}
    dev_name = {d.get("id"): (d.get("name_by_user") or d.get("name") or "") for d in (devices or [])
                if isinstance(d, dict)}
    out = []
    for e in entities or []:
        eid = str((e or {}).get("entity_id") or "")
        if not eid.startswith("assist_satellite.") or e.get("disabled_by"):
            continue
        if (e.get("area_id") or dev_area.get(e.get("device_id"))) == area_id:
            out.append({"entity": eid, "device_id": e.get("device_id"),
                        "name": e.get("name") or dev_name.get(e.get("device_id")) or ""})
    return out


def silence_wav(seconds: float = 0.3, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    w.close()
    return buf.getvalue()


def ensure_silence(www_dir: str) -> bool:
    """The moment of silence the mic 'says' before it listens, written beside the pages Core serves."""
    path = os.path.join(www_dir, SILENCE_NAME)
    want = silence_wav()
    try:
        with open(path, "rb") as fh:
            if fh.read() == want:
                return True
    except OSError:
        pass
    os.makedirs(www_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(want)
    os.replace(tmp, path)
    return True


def audio_seconds(ctype: str, data: bytes) -> float | None:
    """The length of a spoken file, read from the file itself: WAV (header), FLAC (STREAMINFO), MP3 (every frame
    header counted). None when it cannot be read — then nothing is claimed."""
    try:
        if data[:4] == b"RIFF":
            w = wave.open(io.BytesIO(data))
            return w.getnframes() / float(w.getframerate())
        if data[:4] == b"fLaC":
            si = data[8:8 + 34]
            rate = (si[10] << 12) | (si[11] << 4) | (si[12] >> 4)
            total = ((si[13] & 0x0F) << 32) | (si[14] << 24) | (si[15] << 16) | (si[16] << 8) | si[17]
            return total / float(rate) if rate and total else None
        return _mp3_seconds(data)
    except Exception:  # noqa: BLE001
        return None


_MP3_BR = {  # kbps, by (version is MPEG-1, layer III) index
    True: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    False: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]}
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _mp3_seconds(data: bytes) -> float | None:
    i = 0
    if data[:3] == b"ID3" and len(data) > 10:
        i = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    secs, frames, n = 0.0, 0, len(data)
    while i + 4 <= n:
        b1, b2, b3 = data[i + 1], data[i + 2], data[i + 3]
        if data[i] != 0xFF or (b1 & 0xE0) != 0xE0:
            i += 1
            continue
        ver, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        bri, sri, pad = b2 >> 4, (b2 >> 2) & 3, (b2 >> 1) & 1
        if ver == 1 or layer != 1 or sri == 3 or bri in (0, 15):
            i += 1
            continue
        mpeg1 = ver == 3
        br, sr = _MP3_BR[mpeg1][bri] * 1000, _MP3_SR[ver][sri]
        spf = 1152 if mpeg1 else 576
        size = int((spf // 8) * br / sr) + pad
        if size < 4:
            i += 1
            continue
        secs += spf / float(sr)
        frames += 1
        i += size
    return secs if frames else None


def hold_seconds(audio_s) -> float | None:
    """How long the door holds the mic's turn after a question: the speaker starting, then the question itself."""
    if not isinstance(audio_s, (int, float)) or audio_s <= 0:
        return None
    return round(min(SAY_CAP_S, SPEAKER_START_S + float(audio_s)), 2)


def reopen(state_of, start_conversation, mic: str, sleep=time.sleep, now=time.monotonic) -> dict:
    """When the mic's turn ends (the door held it while the question played), open it again if it is idle.
    state_of(entity) -> state string; start_conversation(mic) asks the platform to listen again."""
    out = {"mic": mic}
    t0 = now()
    while state_of(mic) in _BUSY:
        if now() - t0 > SAT_BUSY_MAX_S:
            out["done"] = "mic still busy"
            return out
        sleep(POLL_S)
    out["turn_s"] = round(now() - t0, 2)
    sleep(SETTLE_S)
    st = state_of(mic)
    out["mic_state"] = st
    if st != "idle":
        out["done"] = "mic already listening" if st == "listening" else "mic not idle (%s)" % st
        return out
    try:
        start_conversation(mic)
        out["done"] = "reopened"
    except Exception as e:  # noqa: BLE001 — said, never raised
        out["done"] = "could not reopen: %s" % str(e)[:160]
    return out
