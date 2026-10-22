"""ProOS Cameras — HomeKit Secure Video event recording over the HomeKit Data Stream. Register 970u, Route 2.

Ported from HAP-NodeJS (PR #1132 branch) src/lib/camera/RecordingManagement.ts (Apache-2.0):
  - what we can record (prebuffer, triggers, fMP4 fragment length; video codec/profiles/levels/sizes; audio codecs),
    as the three Supported…RecordingConfiguration TLVs, encoded exactly as HAP-NodeJS's tlv.encode does;
  - the hub's choice (SelectedCameraRecordingConfiguration) parsed and handed to the delegate;
  - recording on/off (Active), camera on/off and snapshot switches (Camera Operating Mode);
  - dataSend/open "ipcamera.recording" → one recording stream at a time: our open answer, then the delegate's
    fMP4 (first the init segment, then fragments), sent in 256 KB chunks with sequence numbers; ended by the hub's
    ack / close, the data stream closing, or a 12 s safety close after the last fragment.
  - state kept across restarts, the chosen configuration only while what we offer is unchanged (same hash).

Not ported: HAP-NodeJS's "admin only" write flags (HAP-python has no such property).
The delegate (an object) provides:
  update_recording_active(bool), update_recording_configuration(cfg or None),
  stream(stream_id, stop: asyncio.Event) -> async iterator of (bytes, is_last),
  close_recording_stream(stream_id, reason or None), and optionally acknowledge_stream(stream_id).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import struct

from . import datastream as D
from . import hapdefs as H

log = logging.getLogger("proos_cameras.recording")

MOTION, DOORBELL = 0x01, 0x02
FRAGMENTED_MP4 = 0x00
H264 = 0x00
BASELINE, MAIN, HIGH = 0, 1, 2
LEVEL3_1, LEVEL3_2, LEVEL4_0 = 0, 1, 2
AAC_LC, AAC_ELD = 0, 1
KHZ_8, KHZ_16, KHZ_24, KHZ_32, KHZ_44_1, KHZ_48 = range(6)
VARIABLE, CONSTANT = 0, 1
MAX_CHUNK = 0x40000
CLOSE_TIMEOUT = 12.0


# ---------- HAP-NodeJS tlv.encode / tlv.decode, exactly ----------

def enc(t, data, *rest) -> bytes:
    out = bytearray()
    if isinstance(data, bool):
        data = bytes([int(data)])
    elif isinstance(data, int):
        data = bytes([data])
    elif isinstance(data, str):
        data = data.encode()
    if isinstance(data, (list, tuple)):
        for i, e in enumerate(data):
            if i:
                out += b"\x00\x00"
            out += enc(t, e)
        if not data:
            out += bytes([t, 0])
    elif len(data) <= 255:
        out += bytes([t, len(data)]) + data
    else:
        i = 0
        while i < len(data):
            part = data[i:i + 255]
            out += bytes([t, len(part)]) + part
            i += 255
    if len(rest) >= 2:
        out += enc(*rest)
    return bytes(out)


def dec(buf: bytes) -> dict:
    out, i = {}, 0
    while i < len(buf):
        if len(buf) - i < 2:
            raise ValueError("TLV decode failure: incomplete type/length header")
        t, n = buf[i], buf[i + 1]
        i += 2
        if n > len(buf) - i:
            raise ValueError("TLV decode failure: declared length exceeds remaining buffer")
        out[t] = out.get(t, b"") + buf[i:i + n]
        i += n
    return out


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


# ---------- what we offer ----------

def supported_camera_config(prebuffer_ms: int, event_triggers: int, containers) -> bytes:
    """containers: [(type, fragment_length_ms)]"""
    return enc(1, struct.pack("<i", prebuffer_ms),
               2, struct.pack("<i", event_triggers) + b"\x00" * 4,
               3, [enc(1, ctype, 2, enc(1, struct.pack("<i", frag))) for ctype, frag in containers])


def supported_video_config(codec: int, profiles, levels, resolutions) -> bytes:
    """resolutions: [(width, height, fps)]"""
    params = enc(1, list(profiles), 2, list(levels))
    attrs = [enc(1, struct.pack("<H", w), 2, struct.pack("<H", h), 3, bytes([fps])) for w, h, fps in resolutions]
    return enc(1, enc(1, codec, 2, params, 3, attrs))


def supported_audio_config(codecs) -> bytes:
    """codecs: [dict(type, channels=1, bitrate_mode=VARIABLE, samplerates=[...])]"""
    if not codecs:
        raise ValueError("At least one audio codec configuration must be specified!")
    confs = []
    for c in codecs:
        rates = c["samplerates"] if isinstance(c["samplerates"], (list, tuple)) else [c["samplerates"]]
        if not rates:
            raise ValueError("Audio samplerate cannot be empty!")
        params = enc(1, max(1, c.get("channels") or 1), 2, c.get("bitrate_mode") or VARIABLE, 3, list(rates))
        confs.append(enc(1, c["type"], 2, params))
    return enc(1, confs)


def parse_selected(value: bytes) -> dict:
    d = dec(value)
    rec, video, audio = dec(d[1]), dec(d[2]), dec(d[3])
    triggers = struct.unpack("<i", rec[2][:4])[0]
    mc = dec(rec[3])
    mcp = dec(mc[2])
    vp, va = dec(video[2]), dec(video[3])
    ap = dec(audio[2])
    typed, bit = [], 0
    while triggers > 0:
        if triggers & 1:
            typed.append(1 << bit)
        triggers >>= 1
        bit += 1
    return {
        "prebuffer_ms": struct.unpack("<i", rec[1][:4])[0],
        "event_triggers": typed,
        "container": {"type": mc[1][0], "fragment_ms": struct.unpack("<i", mcp[1][:4])[0]},
        "video": {"type": video[1][0], "profile": vp[1][0], "level": vp[2][0],
                  "bitrate": struct.unpack("<i", vp[3][:4])[0], "iframe_interval": struct.unpack("<i", vp[4][:4])[0],
                  "width": struct.unpack("<h", va[1][:2])[0], "height": struct.unpack("<h", va[2][:2])[0],
                  "fps": va[3][0]},
        "audio": {"type": audio[1][0], "channels": ap[1][0], "samplerate": ap[3][0], "bitrate_mode": ap[2][0],
                  "bitrate": struct.unpack("<I", ap[4][:4])[0]},
    }


# ---------- recording management ----------

class RecordingManagement:
    def __init__(self, options: dict, delegate, event_triggers: int, server: D.Server, recording_svc=None,
                 operating_mode_svc=None, sensor_services=None, on_state_change=None):
        """options: prebuffer_ms, containers [(type, fragment_ms)], video {codec, profiles, levels, resolutions},
        audio [codec dicts]."""
        self.options, self.delegate, self.server = options, delegate, server
        self.event_triggers = event_triggers
        self.on_state_change = on_state_change or (lambda: None)
        self.sensor_services = list(sensor_services or [])
        self.stream = None
        self.selected = None          # (parsed, base64)
        self.recording_active = False

        self.supported_camera = b64(supported_camera_config(options["prebuffer_ms"], event_triggers, options["containers"]))
        v = options["video"]
        self.supported_video = b64(supported_video_config(v["codec"], v["profiles"], v["levels"], v["resolutions"]))
        self.supported_audio = b64(supported_audio_config(options["audio"]))

        self.svc = recording_svc or H.make_service("CameraRecordingManagement",
                                                   ["RecordingAudioActive", "SelectedCameraRecordingConfiguration",
                                                    "SupportedAudioRecordingConfiguration",
                                                    "SupportedCameraRecordingConfiguration",
                                                    "SupportedVideoRecordingConfiguration"])
        self.op = operating_mode_svc or H.make_service("CameraOperatingMode", ["PeriodicSnapshotsActive"])
        if recording_svc is None:
            H.char(self.svc, "Active").set_value(0)
            H.char(self.svc, "RecordingAudioActive").set_value(0)
            H.char(self.op, "EventSnapshotsActive").set_value(1)
            H.char(self.op, "HomeKitCameraActive").set_value(1)
            H.char(self.op, "PeriodicSnapshotsActive").set_value(1)

        H.char(self.svc, "SupportedCameraRecordingConfiguration").set_value(self.supported_camera)
        H.char(self.svc, "SupportedVideoRecordingConfiguration").set_value(self.supported_video)
        H.char(self.svc, "SupportedAudioRecordingConfiguration").set_value(self.supported_audio)

        sel = H.char(self.svc, "SelectedCameraRecordingConfiguration")
        sel.getter_callback = self._selected_read
        sel.setter_callback = self._selected_write
        H.char(self.svc, "Active").setter_callback = self._active_write
        H.char(self.svc, "RecordingAudioActive").setter_callback = lambda v: self.on_state_change()
        H.char(self.op, "HomeKitCameraActive").setter_callback = self._camera_active_write
        H.char(self.op, "EventSnapshotsActive").setter_callback = lambda v: self.on_state_change()
        H.char(self.op, "PeriodicSnapshotsActive").setter_callback = lambda v: self.on_state_change()

        server.on("dataSend", "request", "open", self._on_open)

    # characteristic handlers
    def _selected_read(self):
        if not self.selected:
            raise RuntimeError("no selected recording configuration yet")     # HAP-python answers communication failure
        return self.selected[1]

    def _selected_write(self, value):
        parsed = parse_selected(base64.b64decode(value))
        changed = not self.selected or self.selected[1] != value
        log.info("recording: hub chose %s", parsed)
        self.selected = (parsed, value)
        if changed:
            self.delegate.update_recording_configuration(parsed)
            self.on_state_change()

    def _active_write(self, value):
        self.on_state_change()
        if bool(value) == self.recording_active:
            return
        self.recording_active = bool(value)
        log.info("recording: recording turned %s in the Home app", "on" if self.recording_active else "off")
        self.delegate.update_recording_active(self.recording_active)

    def _camera_active_write(self, value):
        for s in self.sensor_services:
            if H.has(s, "StatusActive"):
                H.char(s, "StatusActive").set_value(bool(value))
        if not value and self.stream:
            self.stream.close(D.R_NOT_ALLOWED)
        self.on_state_change()

    def camera_active(self) -> bool:
        return bool(H.char(self.op, "HomeKitCameraActive").value)

    # data stream
    def _reject(self, conn, rid, reason):
        conn.send_response("dataSend", "open", rid, D.PROTOCOL_SPECIFIC_ERROR, {"status": reason})

    def _on_open(self, conn, rid, message):
        if not isinstance(message, dict) or message.get("type") != "ipcamera.recording":
            return
        if message.get("target") != "controller":
            return self._reject(conn, rid, D.R_UNEXPECTED_FAILURE)
        if not self.recording_active:
            log.info("recording: refused — recording is off")
            return self._reject(conn, rid, D.R_NOT_ALLOWED)
        if not self.camera_active():
            log.info("recording: refused — camera is off in the Home app")
            return self._reject(conn, rid, D.R_NOT_ALLOWED)
        if self.stream:
            log.info("recording: refused — another recording is running (stream %s)", self.stream.stream_id)
            return self._reject(conn, rid, D.R_BUSY)
        if not self.selected:
            log.info("recording: refused — the hub has not chosen a recording configuration")
            return self._reject(conn, rid, D.R_INVALID_CONFIGURATION)
        log.info("recording: hub opened stream %s (reason %s)", message.get("streamId"), message.get("reason"))
        self.stream = RecordingStream(conn, self.delegate, rid, message.get("streamId"), self._stream_closed)
        self.stream.start()

    def _stream_closed(self, stream):
        if self.stream is stream:
            self.stream = None

    # persistence
    def config_hash(self) -> str:
        h = hashlib.sha256()
        for s in (self.supported_camera, self.supported_video, self.supported_audio):
            h.update(s.encode())
        return h.hexdigest()

    def serialize(self) -> dict:
        return {"configurationHash": {"algorithm": "sha256", "hash": self.config_hash()},
                "selectedConfiguration": self.selected[1] if self.selected else None,
                "recordingActive": self.recording_active,
                "recordingAudioActive": bool(H.char(self.svc, "RecordingAudioActive").value),
                "eventSnapshotsActive": bool(H.char(self.op, "EventSnapshotsActive").value),
                "homeKitCameraActive": bool(H.char(self.op, "HomeKitCameraActive").value),
                "periodicSnapshotsActive": bool(H.char(self.op, "PeriodicSnapshotsActive").value)}

    def deserialize(self, s: dict):
        changed = False
        if s.get("selectedConfiguration"):
            if (s.get("configurationHash") or {}).get("hash") == self.config_hash():
                v = s["selectedConfiguration"]
                self.selected = (parse_selected(base64.b64decode(v)), v)
            else:
                log.info("recording: saved choice discarded — what we offer has changed since")
                changed = True
        self.recording_active = bool(s.get("recordingActive"))
        H.char(self.svc, "Active").set_value(int(self.recording_active))
        H.char(self.svc, "RecordingAudioActive").set_value(int(bool(s.get("recordingAudioActive"))))
        H.char(self.op, "EventSnapshotsActive").set_value(int(bool(s.get("eventSnapshotsActive", True))))
        H.char(self.op, "PeriodicSnapshotsActive").set_value(int(bool(s.get("periodicSnapshotsActive", True))))
        cam = bool(s.get("homeKitCameraActive", True))
        H.char(self.op, "HomeKitCameraActive").set_value(int(cam))
        for sv in self.sensor_services:
            if H.has(sv, "StatusActive"):
                H.char(sv, "StatusActive").set_value(cam)
        try:
            if self.selected:
                self.delegate.update_recording_configuration(self.selected[0])
            if self.recording_active:
                self.delegate.update_recording_active(True)
        except Exception:
            log.exception("recording: delegate failed restoring saved state")
        if changed:
            self.on_state_change()

    def factory_reset(self):
        self.selected = None
        self.recording_active = False
        H.char(self.svc, "Active").set_value(0)
        H.char(self.svc, "RecordingAudioActive").set_value(0)
        H.char(self.op, "EventSnapshotsActive").set_value(1)
        H.char(self.op, "PeriodicSnapshotsActive").set_value(1)
        H.char(self.op, "HomeKitCameraActive").set_value(1)
        for sv in self.sensor_services:
            if H.has(sv, "StatusActive"):
                H.char(sv, "StatusActive").set_value(True)
        try:
            self.delegate.update_recording_active(False)
            self.delegate.update_recording_configuration(None)
        except Exception:
            log.exception("recording: delegate failed after factory reset")

    def destroy(self):
        self.server.off("dataSend", "request", "open", self._on_open)
        if self.stream:
            self.stream.close(D.R_CANCELLED)


class RecordingStream:
    def __init__(self, conn: D.Connection, delegate, rid, stream_id, on_closed):
        self.conn, self.delegate, self.rid, self.stream_id = conn, delegate, rid, stream_id
        self.on_closed = on_closed
        self.closed = False
        self.stop = asyncio.Event()
        self.task = None
        self.close_timer = None
        self.handler = {"event": {"close": self._on_close_event, "ack": self._on_ack}}
        conn.on_close.append(self._conn_closed)
        conn.add_protocol_handler("dataSend", self.handler)

    def start(self):
        self.task = asyncio.ensure_future(self._run(), loop=self.conn.loop)

    async def _run(self):
        self.conn.send_response("dataSend", "open", self.rid, D.SUCCESS, {"status": D.SUCCESS})
        init, seq, last_marked = True, 1, False
        agen = None
        try:
            agen = self.delegate.stream(self.stream_id, self.stop).__aiter__()
            async for data, is_last in agen:
                if self.closed:
                    break
                if last_marked:
                    log.error("recording: delegate yielded after signalling the end of stream %s", self.stream_id)
                    break
                off, chunk = 0, 1
                while off < len(data):
                    if self.closed:
                        break
                    part = data[off:off + MAX_CHUNK]
                    off += len(part)
                    meta = {"dataType": "mediaInitialization" if init else "mediaFragment",
                            "dataSequenceNumber": seq, "dataChunkSequenceNumber": chunk,
                            "isLastDataChunk": off >= len(data)}
                    if chunk == 1:
                        meta["dataTotalSize"] = len(data)
                    event = {"streamId": self.stream_id, "packets": [{"data": part, "metadata": meta}]}
                    if off >= len(data):
                        event["endOfStream"] = bool(is_last)
                    self.conn.send_event("dataSend", "data", event)
                    if seq <= 2 and chunk == 1:
                        log.info("recording: stream %s — %s %d sent to the hub (%d bytes)", self.stream_id,
                                 meta["dataType"], seq, len(data))
                    chunk += 1
                    init = False
                last_marked = bool(is_last)
                if is_last:
                    break
                seq += 1
            if not last_marked and not self.closed:
                log.warning("recording: stream %s ended without marking its last fragment", self.stream_id)
        except ConnectionError:
            log.info("recording: data stream closed under stream %s", self.stream_id)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self.closed:
                reason = getattr(e, "reason", D.R_UNEXPECTED_FAILURE)
                log.exception("recording: stream %s failed", self.stream_id)
                self.close(reason)
            return
        finally:
            if agen is not None and hasattr(agen, "aclose"):
                try:
                    await agen.aclose()
                except Exception:
                    pass
            if not self.closed:
                self._arm_close()
        log.info("recording: finished sending stream %s", self.stream_id)

    def _on_ack(self, message):
        log.info("recording: hub ack for stream %s: %r", self.stream_id, message)
        log.info("recording: hub acknowledged stream %s", self.stream_id)
        ack = getattr(self.delegate, "acknowledge_stream", None)
        self._closed(lambda: ack(self.stream_id) if ack else None)

    def _on_close_event(self, message):
        if (message or {}).get("streamId") != self.stream_id:
            return
        reason = (message or {}).get("reason")
        log.info("recording: hub closed stream %s (reason %s)", self.stream_id, reason)
        self._closed(lambda: self.delegate.close_recording_stream(self.stream_id, reason))

    def _conn_closed(self, _conn):
        self._closed(lambda: self.delegate.close_recording_stream(self.stream_id, None))

    def _closed(self, closure):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.close_timer:
            self.close_timer.cancel()
            self.close_timer = None
        self.conn.remove_protocol_handler("dataSend", self.handler)
        if self._conn_closed in self.conn.on_close:
            self.conn.on_close.remove(self._conn_closed)
        try:
            closure()
        except Exception:
            log.exception("recording: delegate failed handling the close of stream %s", self.stream_id)
        self.on_closed(self)

    def close(self, reason):
        if self.closed:
            return
        log.info("recording: closing stream %s (reason %s)", self.stream_id, reason)
        if not self.conn.is_closed():
            try:
                self.conn.send_event("dataSend", "close", {"streamId": self.stream_id, "reason": reason})
            except Exception:
                pass
        self._closed(lambda: self.delegate.close_recording_stream(self.stream_id, reason))

    def _arm_close(self):
        if self.close_timer:
            self.close_timer.cancel()

        def late():
            if not self.closed:
                log.info("recording: stream %s took too long to close — closing now", self.stream_id)
                self.close(D.R_CANCELLED)
        self.close_timer = self.conn.loop.call_later(CLOSE_TIMEOUT, late)
