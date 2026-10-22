"""ProOS Cameras — the live view's sound, both ways. Register 970t, stage B.

OUT (camera → iPhone): ffmpeg encodes the camera's microphone as Opus RTP to a local port; this relay corrects the
timestamps to Apple's clock and encrypts it as SRTP — the work Home Assistant's own Apple Home cameras hand to
homekit-audio-proxy (Apache-2.0), whose SRTP encryption is ported here unchanged in method.

BACK (iPhone → door): Apple sends the iPhone's microphone to the SAME port the accessory sends from (that is why the
platform's proxy binds the iPhone's audio port number as its source). The platform never reads it — its doorbell's
Speaker is for show. This relay reads it, verifies and decrypts it with the session's key (the accessory answers
with the controller's own SRTP key, so one key serves both directions), puts the timestamps back on Opus's 48 kHz
clock, and hands plain RTP to ffmpeg, which sends it to the camera through Ubiquiti's official talk session — the
path a test tone took to Dave's Front Door on 7 Oct (register 700–707).

Nothing is recorded: sound passes through.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import socket
import struct
import threading

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

OPUS_CLOCK = 48000
TAG = 10
_U32 = struct.Struct("!I")
_U16 = struct.Struct("!H")


def _derive(master_key: bytes, master_salt: bytes, label: int, length: int) -> bytes:
    """RFC 3711 §4.3.1 session key derivation (as homekit-audio-proxy)."""
    x = (label << 48) ^ int.from_bytes(master_salt, "big")
    iv = x.to_bytes(14, "big") + b"\x00\x00"
    enc = Cipher(algorithms.AES(master_key), modes.CTR(iv)).encryptor()
    return (enc.update(b"\x00" * length) + enc.finalize())[:length]


def _header_len(pkt: bytes) -> int:
    n = 12 + (pkt[0] & 0x0F) * 4
    if (pkt[0] >> 4) & 1:
        n += 4 + _U16.unpack_from(pkt, n + 2)[0] * 4
    return n


class SRTP:
    """AES_CM_128_HMAC_SHA1_80 — one direction's context (its own rollover counter)."""

    def __init__(self, key_b64: str):
        km = base64.b64decode(key_b64)
        if len(km) < 30:
            raise ValueError("SRTP key material must be 30 bytes")
        mk, ms = km[:16], km[16:30]
        self.k = _derive(mk, ms, 0, 16)
        self.a = _derive(mk, ms, 1, 20)
        self.s = _derive(mk, ms, 2, 14)
        self.roc, self.last = 0, None

    def _iv(self, ssrc: int, index: int) -> bytes:
        iv = bytearray(16)
        _U32.pack_into(iv, 4, ssrc)
        struct.pack_into("!HI", iv, 8, index >> 32, index & 0xFFFFFFFF)
        for i in range(14):
            iv[i] ^= self.s[i]
        return bytes(iv)

    def _index(self, seq: int) -> int:
        if self.last is not None and seq < self.last and (self.last - seq) > 0x8000:
            self.roc += 1
        self.last = seq
        return (self.roc << 16) | seq

    def encrypt(self, pkt: bytes) -> bytes:
        h = _header_len(pkt)
        ssrc, seq = _U32.unpack_from(pkt, 8)[0], _U16.unpack_from(pkt, 2)[0]
        idx = self._index(seq)
        enc = Cipher(algorithms.AES(self.k), modes.CTR(self._iv(ssrc, idx))).encryptor()
        out = pkt[:h] + enc.update(pkt[h:]) + enc.finalize()
        tag = hmac.new(self.a, out + _U32.pack(self.roc), hashlib.sha1).digest()[:TAG]
        return out + tag

    def decrypt(self, pkt: bytes) -> bytes | None:
        """The plain RTP packet, or None when it fails authentication (never passed on)."""
        if len(pkt) < 12 + TAG:
            return None
        body, tag = pkt[:-TAG], pkt[-TAG:]
        seq = _U16.unpack_from(body, 2)[0]
        roc = self.roc
        if self.last is not None and seq < self.last and (self.last - seq) > 0x8000:
            roc += 1
        if not hmac.compare_digest(hmac.new(self.a, body + _U32.pack(roc), hashlib.sha1).digest()[:TAG], tag):
            return None
        idx = self._index(seq)
        h = _header_len(body)
        dec = Cipher(algorithms.AES(self.k), modes.CTR(self._iv(_U32.unpack_from(body, 8)[0], idx))).decryptor()
        return body[:h] + dec.update(body[h:]) + dec.finalize()


def is_rtcp(pkt: bytes) -> bool:
    return len(pkt) >= 2 and 200 <= pkt[1] <= 204


def rescale(pkt: bytes, num: int, den: int) -> bytes:
    ts = _U32.unpack_from(pkt, 4)[0]
    out = bytearray(pkt)
    _U32.pack_into(out, 4, (ts * num // den) & 0xFFFFFFFF)
    return bytes(out)


def talk_sdp(port: int, pt: int = 110) -> str:
    """What ffmpeg reads the iPhone's (now plain) Opus from."""
    return ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=ProOS talk\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            "m=audio %d RTP/AVP %d\r\na=rtpmap:%d opus/48000/2\r\na=fmtp:%d stereo=0;sprop-stereo=0\r\n"
            % (port, pt, pt, pt))


_CODECS = {"opus": ["-c:a", "libopus", "-b:a", "48k", "-application", "voip", "-frame_duration", "20"],
           "aac": ["-c:a", "aac", "-b:a", "64k"]}      # Core's protecttalk.py, the same encoder settings


def ffmpeg_talk(sdp_path: str, session: dict) -> list:
    """The iPhone's voice in (plain RTP via the SDP), the camera's codec out, RTP to Protect's talk address."""
    url = str(session.get("url") or "")
    enc = _CODECS.get(str(session.get("codec") or "").lower())
    if not url.startswith("rtp://") or not enc:
        raise ValueError("Protect gave a talk session ProOS does not recognise")
    rate = int(session.get("samplingRate") or 24000)
    return ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-protocol_whitelist", "file,udp,rtp",
            "-f", "sdp", "-i", sdp_path, "-ac", "1", "-ar", str(rate)] + enc + ["-f", "rtp", url]


class Relay:
    """One live view's sound. Bound on the iPhone's audio port number (as the platform's proxy), so Apple's return
    audio arrives here. `on_voice()` is called once, on the first real packet of the iPhone's voice, so the door's
    talk session is opened only when someone actually talks."""

    def __init__(self, dest_addr: str, dest_port: int, key_b64: str, rate_khz: int, on_voice=None):
        self.dest = (dest_addr, dest_port)
        self.rate = int(rate_khz or 16) * 1000
        self.tx, self.rx = SRTP(key_b64), SRTP(key_b64)
        self.on_voice, self.voice_port, self.voiced = on_voice, None, False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", dest_port))
        self.local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.local.bind(("127.0.0.1", 0))
        self.local_port = self.local.getsockname()[1]
        self.fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.stopped = threading.Event()
        self.packets_out = self.packets_back = 0

    def start(self):
        for f in (self._out, self._back):
            threading.Thread(target=f, daemon=True).start()

    def stop(self):
        self.stopped.set()
        for s in (self.sock, self.local, self.fwd):
            try:
                s.close()
            except OSError:
                pass

    def out_packet(self, pkt: bytes) -> bytes:
        return self.tx.encrypt(rescale(pkt, self.rate, OPUS_CLOCK))

    def back_packet(self, pkt: bytes) -> bytes | None:
        if is_rtcp(pkt):
            return None
        plain = self.rx.decrypt(pkt)
        return rescale(plain, OPUS_CLOCK, self.rate) if plain else None

    def _out(self):
        self.local.settimeout(1.0)
        sender = None
        while not self.stopped.is_set():
            try:
                data, frm = self.local.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if len(data) < 12:
                continue
            if sender is None:
                sender = frm               # locked to ffmpeg, as the platform's proxy does
            elif frm != sender:
                continue
            try:
                self.sock.sendto(self.out_packet(data), self.dest)
                self.packets_out += 1
            except OSError:
                pass

    def _back(self):
        self.sock.settimeout(1.0)
        while not self.stopped.is_set():
            try:
                data, frm = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if frm[0] != self.dest[0]:
                continue                    # only the iPhone this live view belongs to
            plain = self.back_packet(data)
            if not plain:
                continue
            if not self.voiced:
                self.voiced = True
                if self.on_voice:
                    try:
                        self.voice_port = self.on_voice()
                    except Exception:  # noqa: BLE001 — no talk session: the live view carries on
                        self.voice_port = None
            if self.voice_port:
                try:
                    self.fwd.sendto(plain, ("127.0.0.1", self.voice_port))
                    self.packets_back += 1
                except OSError:
                    pass
