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
speaker, ProOS WATCHES the speaker (physical state, not an estimate) until it stops playing, waits for the mic to
finish its own turn, and — only if the mic is idle (a mic that reopened by itself, like the G-Flip panel's, is left
alone) — starts a conversation on it with a moment of silence, so nothing is said twice.

TIMINGS below are plumbing: how long to wait for things that report their own state.
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

SPK_START_S = 4.0       # the room speaker beginning to play the question
SPK_MAX_S = 40.0        # a question never plays longer than this
SAT_BUSY_MAX_S = 25.0   # the mic finishing its own turn (the door holds the turn while the question plays)
POLL_S = 0.3
SETTLE_S = 0.6          # after the mic goes idle, a moment for one that reopens by itself to do so

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


def reopen(state_of, start_conversation, speaker: str, mic: str, sleep=time.sleep, now=time.monotonic) -> dict:
    """After a question played through `speaker`, open `mic` again. Returns what was seen and done.
    state_of(entity) -> state string; start_conversation(mic) asks the platform to listen again."""
    out = {"speaker": speaker, "mic": mic}
    t0 = now()
    # 1 · the speaker starts playing the question …
    played = False
    while now() - t0 < SPK_START_S:
        if state_of(speaker) == "playing":
            played = True
            break
        sleep(POLL_S)
    out["speaker_played"] = played
    # 2 · … and stops
    if played:
        t1 = now()
        while state_of(speaker) == "playing":
            if now() - t1 > SPK_MAX_S:
                out["speaker_still_playing"] = True
                break
            sleep(POLL_S)
    # 3 · the mic finishes its own turn
    t2 = now()
    while state_of(mic) in _BUSY:
        if now() - t2 > SAT_BUSY_MAX_S:
            out["done"] = "mic still busy"
            return out
        sleep(POLL_S)
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
