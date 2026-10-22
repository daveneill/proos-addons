"""ProOS Cameras — Apple's Secure Video message formats (TLV8). Register 970u, Route 2.

Ported from HAP-NodeJS PR #1132 "HomeKit Secure Video 3 (iOS 27)" (src/lib/camera/SecureVideoTypes.ts, Apache-2.0,
by seydx), which its author tested against a real Apple home hub; field numbers and widths kept exactly. Apple's
HomeKit Secure Video Open Source Compatibility Guide (developer preview, 3 Jun 2026) is the public reference.

Only the formats the first box build needs are here (capabilities, stream tiers, sensor ids, the multi-tier RTP
control and setup, session status, zones). WebRTC and the CMAF direct-upload formats come with their stages.
"""
from __future__ import annotations

import struct
import uuid as _uuid

CAMERA_CAPABILITIES_VERSION = "17.99"
DELIMITER = b"\x00\x00"

# StreamTierVideoCodec / StreamTierAudioCodec / CameraVideoQuality / sample rate / bit depth / sensor type & intent
H264, H265 = 1, 2
OPUS = 3
HIGHEST, HIGH, MEDIUM, LOW = 1, 2, 3, 4
KHZ_16, KHZ_24, KHZ_32, KHZ_48 = 1, 2, 3, 4
BITS_8, BITS_16, BITS_24 = 1, 2, 3
SENSOR_PRIMARY, SENSOR_GENERIC = 1, 255
INTENT_MAIN, INTENT_PACKAGE = 1, 2
# RTPStreamingCommand / RTPStreamingStatus
RTP_END, RTP_START = 1, 2
RTP_SUCCESS, RTP_UNKNOWN_SESSION, RTP_NO_SUCH_STREAM, RTP_BUSY, RTP_ERROR = 0, 1, 2, 3, 4


class TLVError(ValueError):
    pass


def _le(b: bytes) -> int:
    return int.from_bytes(b, "little")


class TLVReader:
    """Entries in order; a full 255-byte fragment continues into the next entry of the same type; a 00 00 entry is a
    list delimiter (the next entry of the same type starts a new item)."""

    def __init__(self, buf: bytes):
        self.entries = []
        i, after_delim, last = 0, False, None
        while i < len(buf):
            if i + 2 > len(buf):
                raise TLVError("TLV decode failure: incomplete type/length header")
            t, n = buf[i], buf[i + 1]
            if i + 2 + n > len(buf):
                raise TLVError("TLV decode failure: declared length %d exceeds remaining buffer" % n)
            v = bytes(buf[i + 2:i + 2 + n])
            i += 2 + n
            if t == 0 and n == 0:
                after_delim = True
                continue
            if last is not None and not after_delim and last[0] == t and last[2] == 255:
                last[1] += v
                last[2] = n
                continue
            last = [t, v, n]
            self.entries.append(last)
            after_delim = False

    def has(self, t):
        return any(e[0] == t for e in self.entries)

    def buffer(self, t):
        return next((e[1] for e in self.entries if e[0] == t), None)

    def buffers(self, t):
        return [e[1] for e in self.entries if e[0] == t]

    def string(self, t):
        b = self.buffer(t)
        return b.decode("utf-8") if b is not None else None

    def number(self, t):
        b = self.buffer(t)
        return _le(b) if b else None

    def boolean(self, t):
        b = self.buffer(t)
        return (b[0] != 0) if b else None

    def uuid(self, t):
        b = self.buffer(t)
        return str(_uuid.UUID(bytes=b)) if b is not None and len(b) == 16 else None

    @staticmethod
    def required(v, name):
        if v is None:
            raise TLVError("TLV decode failure: missing %s" % name)
        return v


def item(t: int, v: bytes | None) -> bytes:
    if v is None:
        return b""
    out, off = [], 0
    while True:
        chunk = v[off:off + 255]
        out.append(bytes([t, len(chunk)]) + chunk)
        off += 255
        if off >= len(v):
            break
    return b"".join(out)


def tlist(t: int, values) -> bytes:
    out = []
    for i, v in enumerate(values):
        if i:
            out.append(DELIMITER)
        out.append(item(t, v))
    return b"".join(out)


def u8(v):
    return bytes([v & 0xFF])


def u16(v):
    return struct.pack("<H", v & 0xFFFF)


def u32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def u64(v):
    return struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)


def ubool(v):
    return b"\x01" if v else b"\x00"


def uuid_bytes(s: str) -> bytes:
    return bytes.fromhex(s.replace("-", ""))


# ── stream tiers ────────────────────────────────────────────────────────────────────────────────────────────────
def encode_video_tiers(codec: int, payload_type: int, tiers: list) -> bytes:
    """tiers: [{identifier, quality, targetAverageBitrate (kbps), width, height, frameRate}]"""
    return (item(1, u8(codec)) + item(2, u8(payload_type)) + tlist(3, [
        item(1, u32(t["identifier"])) + item(2, u8(t["quality"])) + item(3, u32(t["targetAverageBitrate"]))
        + item(4, u16(t["width"])) + item(5, u16(t["height"])) + item(6, u8(t["frameRate"])) for t in tiers]))


def decode_video_tiers(buf: bytes) -> dict:
    r = TLVReader(buf)
    out = {"codec": r.required(r.number(1), "codec"), "payloadType": r.required(r.number(2), "payload type"),
           "tiers": []}
    for e in r.buffers(3):
        t = TLVReader(e)
        out["tiers"].append({k: t.required(t.number(i), "tier " + k) for i, k in
                             ((1, "identifier"), (2, "quality"), (3, "targetAverageBitrate"), (4, "width"),
                              (5, "height"), (6, "frameRate"))})
    return out


def encode_audio_tiers(codec: int, payload_type: int, tiers: list) -> bytes:
    """tiers: [{identifier, targetAverageBitrate, sampleRate, bitDepth, packetTime, channels}]"""
    return (item(1, u8(codec)) + item(2, u8(payload_type)) + tlist(3, [
        item(1, u32(t["identifier"])) + item(2, u32(t["targetAverageBitrate"])) + item(3, u8(t["sampleRate"]))
        + item(4, u8(t["bitDepth"])) + item(5, u8(t["packetTime"])) + item(6, u8(t["channels"])) for t in tiers]))


def decode_audio_tiers(buf: bytes) -> dict:
    r = TLVReader(buf)
    out = {"codec": r.required(r.number(1), "codec"), "payloadType": r.required(r.number(2), "payload type"),
           "tiers": []}
    for e in r.buffers(3):
        t = TLVReader(e)
        out["tiers"].append({k: t.required(t.number(i), "tier " + k) for i, k in
                             ((1, "identifier"), (2, "targetAverageBitrate"), (3, "sampleRate"), (4, "bitDepth"),
                              (5, "packetTime"), (6, "channels"))})
    return out


# ── capabilities ────────────────────────────────────────────────────────────────────────────────────────────────
def encode_capabilities(version: int, sensors: list) -> bytes:
    """sensors: [{width, height, sensorUUID, type, intent, videoStreams: [{identifier (uuid), quality, width, height,
    frameRate, averageBitrate, peakBitrate}]}]"""
    enc = []
    for s in sensors:
        enc.append(item(1, item(1, u16(s["width"])) + item(2, u16(s["height"])))
                   + item(2, uuid_bytes(s["sensorUUID"])) + item(3, u8(s["type"])) + item(4, u8(s["intent"]))
                   + tlist(5, [item(1, uuid_bytes(v["identifier"])) + item(2, u8(v["quality"]))
                               + item(3, u16(v["width"])) + item(4, u16(v["height"])) + item(5, u8(v["frameRate"]))
                               + item(6, u32(v["averageBitrate"])) + item(7, u32(v["peakBitrate"]))
                               for v in s["videoStreams"]]))
    return item(1, u8(version)) + item(2, tlist(1, enc))


def decode_capabilities(buf: bytes) -> dict:
    r = TLVReader(buf)
    sensors = TLVReader(r.buffer(2) or b"")
    out = {"version": r.required(r.number(1), "version"), "sensors": []}
    for e in sensors.buffers(1):
        s = TLVReader(e)
        dim = TLVReader(s.required(s.buffer(1), "sensor dimensions"))
        streams = []
        for se in s.buffers(5):
            v = TLVReader(se)
            streams.append({"identifier": v.required(v.uuid(1), "stream identifier"),
                            **{k: v.required(v.number(i), "stream " + k) for i, k in
                               ((2, "quality"), (3, "width"), (4, "height"), (5, "frameRate"), (6, "averageBitrate"),
                                (7, "peakBitrate"))}})
        out["sensors"].append({"width": dim.required(dim.number(1), "sensor width"),
                               "height": dim.required(dim.number(2), "sensor height"),
                               "sensorUUID": s.required(s.uuid(2), "sensor uuid"),
                               "type": s.number(3) or 0, "intent": s.number(4) or 0, "videoStreams": streams})
    return out


def encode_contributing_sensors(uuids: list) -> bytes:
    return tlist(1, [item(1, uuid_bytes(u)) for u in uuids])


def encode_sensor_uuid(u: str) -> bytes:
    return uuid_bytes(u)


# ── multi-tier RTP ──────────────────────────────────────────────────────────────────────────────────────────────
def decode_rtp_streaming_control(buf: bytes) -> dict:
    r = TLVReader(buf)
    return {"sessionIdentifier": r.required(r.uuid(1), "session identifier"),
            "command": r.required(r.number(2), "command"),
            "videoTier": r.number(3), "videoSSRC": r.number(4), "audioTier": r.number(5), "audioSSRC": r.number(6)}


def encode_session_status(session_id: str | None, status: int) -> bytes:
    return (item(1, uuid_bytes(session_id)) if session_id else b"") + item(2, u8(status))


def _dec_addr(buf):
    r = TLVReader(buf)
    return {"version": "ipv6" if r.number(1) == 1 else "ipv4", "address": r.required(r.string(2), "address"),
            "videoRTPPort": r.required(r.number(3), "video rtp port"),
            "audioRTPPort": r.required(r.number(4), "audio rtp port")}


def _enc_addr(a):
    return (item(1, u8(1 if a["version"] == "ipv6" else 0)) + item(2, a["address"].encode())
            + item(3, u16(a["videoRTPPort"])) + item(4, u16(a["audioRTPPort"])))


def _dec_srtp(buf):
    r = TLVReader(buf)
    return {"cryptoSuite": r.number(1) or 0, "masterKey": r.buffer(2) or b"", "masterSalt": r.buffer(3) or b""}


def _enc_srtp(p):
    return item(1, u8(p["cryptoSuite"])) + item(2, p["masterKey"]) + item(3, p["masterSalt"])


def decode_rtp_setup_endpoints(buf: bytes) -> dict:
    """The multi-tier Setup Endpoints reuses the legacy HAP Setup Endpoints TLV."""
    r = TLVReader(buf)
    return {"sessionIdentifier": r.required(r.uuid(1), "session identifier"),
            "controllerAddress": _dec_addr(r.required(r.buffer(3), "controller address")),
            "video": _dec_srtp(r.required(r.buffer(4), "video srtp parameters")),
            "audio": _dec_srtp(r.required(r.buffer(5), "audio srtp parameters"))}


def encode_rtp_setup_endpoints_response(resp: dict) -> bytes:
    return (item(1, uuid_bytes(resp["sessionIdentifier"])) + item(2, u8(resp.get("status") or 0))
            + item(3, _enc_addr(resp["accessoryAddress"])) + item(4, _enc_srtp(resp["video"]))
            + item(5, _enc_srtp(resp["audio"])) + item(6, u32(resp["videoSSRC"])) + item(7, u32(resp["audioSSRC"])))


# ── motion zones ────────────────────────────────────────────────────────────────────────────────────────────────
def encode_zones(version: int, zones: list) -> bytes:
    enc = []
    for z in zones:
        polys = []
        for p in z["polygons"]:
            verts = b"".join(struct.pack("<HH", v["x"], v["y"]) for v in p["vertices"])
            polys.append(item(1, uuid_bytes(p["identifier"])) + item(3, verts))
        enc.append(item(1, u8(z["method"])) + tlist(3, polys))
    return item(1, u8(version)) + tlist(2, enc)


def decode_zones(buf: bytes) -> dict:
    r = TLVReader(buf)
    out = {"version": r.required(r.number(1), "zone data version"), "zones": []}
    for e in r.buffers(2):
        z = TLVReader(e)
        polys = []
        for pe in z.buffers(3):
            p = TLVReader(pe)
            vd = p.buffer(3) or b""
            polys.append({"identifier": p.uuid(1) or "",
                          "vertices": [{"x": struct.unpack_from("<H", vd, o)[0], "y": struct.unpack_from("<H", vd, o + 2)[0]}
                                       for o in range(0, len(vd) - 3, 4)]})
        out["zones"].append({"method": z.number(1) or 1, "polygons": polys})
    return out


# ── WebRTC (status-only answers until ProOS's WebRTC is built) ─────────────────────────────────────────────────
WEBRTC_OFFER_SUCCESS, WEBRTC_OFFER_PRIVACY_MODE_ACTIVE, WEBRTC_OFFER_ERROR = 0, 1, 2
WEBRTC_SUCCESS, WEBRTC_UNKNOWN_SESSION, WEBRTC_BUSY, WEBRTC_ERROR = 0, 1, 2, 3


def encode_webrtc_solicit_offer_status(status: int) -> bytes:
    """HAP-NodeJS encodeWebRTCSolicitOfferResponse({status}) — no session, offer or candidates: only the status item."""
    return item(4, u8(status))


def decode_session_identifier(buf: bytes):
    """The session identifier (tag 1) every WebRTC request starts with; None if absent."""
    return TLVReader(buf).uuid(1)
