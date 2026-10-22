"""ProOS Cameras — cutting ffmpeg's fragmented MP4 into the pieces Secure Video recording works in. Register 970y.

ffmpeg writes (with -movflags default_base_moof+empty_moov+frag_keyframe+skip_sidx+skip_trailer, the settings of the
Homebridge UniFi Protect plugin's homebridge-plugin-utils 2.4.0 record.js): ftyp + moov (the "init" piece, sent first
as mediaInitialization), then moof + mdat pairs (each a "media" piece). This reads them off a byte stream and tells
whether a media piece starts on a key frame (its first sample is a sync sample), which is where a clip may begin.
"""
from __future__ import annotations

import struct


class Cutter:
    """Feed bytes; collect ('init', bytes) once, then ('media', bytes, starts_with_key_frame)."""

    def __init__(self):
        self.buf = bytearray()
        self.init = None
        self._pending = []          # ftyp/moov boxes until the init is complete
        self._moof = None

    def feed(self, data: bytes) -> list:
        self.buf += data
        out = []
        while len(self.buf) >= 8:
            size, kind = struct.unpack(">I4s", self.buf[:8])
            hdr = 8
            if size == 1:
                if len(self.buf) < 16:
                    break
                size = struct.unpack(">Q", self.buf[8:16])[0]
                hdr = 16
            if size == 0 or size < hdr:
                raise ValueError("unbounded or broken MP4 box %r" % kind)
            if len(self.buf) < size:
                break
            box = bytes(self.buf[:size])
            del self.buf[:size]
            if self.init is None:
                self._pending.append(box)
                if kind == b"moov":
                    self.init = b"".join(self._pending)
                    self._pending = []
                    out.append(("init", self.init))
                continue
            if kind == b"moof":
                self._moof = box
            elif kind == b"mdat" and self._moof is not None:
                out.append(("media", self._moof + box, starts_with_key_frame(self._moof)))
                self._moof = None
        return out


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            hdr = 16
        if size < hdr:
            return
        yield kind, i + hdr, i + size
        i += size


def starts_with_key_frame(moof: bytes) -> bool:
    """The first sample of the first video track fragment is a sync sample (ISO 14496-12: sample_is_non_sync_sample,
    bit 0x00010000 of the sample flags, clear). Flags come from trun's first-sample flags, else its per-sample flags,
    else tfhd's default flags. Only the first traf is read — ffmpeg writes the video track first (it is mapped first)."""
    for kind, s, e in _boxes(moof):
        if kind != b"moof":
            continue
        for k2, s2, e2 in _boxes(moof, s, e):
            if k2 != b"traf":
                continue
            default_flags = None
            for k3, s3, e3 in _boxes(moof, s2, e2):
                if k3 == b"tfhd":
                    fl = struct.unpack(">I", moof[s3:s3 + 4])[0] & 0xFFFFFF
                    p = s3 + 8                     # version/flags + track_ID
                    if fl & 0x01:
                        p += 8
                    if fl & 0x02:
                        p += 4
                    if fl & 0x08:
                        p += 4
                    if fl & 0x10:
                        p += 4
                    if fl & 0x20:
                        default_flags = struct.unpack(">I", moof[p:p + 4])[0]
                elif k3 == b"trun":
                    fl = struct.unpack(">I", moof[s3:s3 + 4])[0] & 0xFFFFFF
                    count = struct.unpack(">I", moof[s3 + 4:s3 + 8])[0]
                    p = s3 + 8
                    if fl & 0x01:
                        p += 4
                    if fl & 0x04:
                        return not (struct.unpack(">I", moof[p:p + 4])[0] & 0x00010000)
                    if count and fl & 0x400:
                        q = p + (4 if fl & 0x100 else 0) + (4 if fl & 0x200 else 0)
                        return not (struct.unpack(">I", moof[q:q + 4])[0] & 0x00010000)
                    if default_flags is not None:
                        return not (default_flags & 0x00010000)
                    return False
            return False
    return False


# ---------- Secure Video recording pieces (970cc) ----------
# Ported from the camera.ui HomeKit plugin (utils/fmp4.ts — the implementation HAP-NodeJS PR #1132's author runs):
# a clip's pieces have their decode times re-based to start at zero per track, and each carries a 'prft' box (the
# wall clock it was recorded at) before its moof — the author: "tvOS 27 hubs read the fragment wall clock from prft
# and crash homed when it is missing".

NTP_EPOCH_OFFSET = 2208988800


def _children(buf, start, end):
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[i:i + 8])
        if size < 8 or i + size > end:
            return
        yield kind, i, i + 8, i + size
        i += size


def rebase_tfdt(piece: bytes, offsets: dict) -> bytes:
    """Each track's decode time made relative to the first one seen in this clip (offsets: track id → first)."""
    out = bytearray(piece)
    for kind, _s, c, e in _children(out, 0, len(out)):
        if kind != b"moof":
            continue
        for k2, _s2, c2, e2 in _children(out, c, e):
            if k2 != b"traf":
                continue
            track, tfdt = None, None
            for k3, s3, c3, _e3 in _children(out, c2, e2):
                if k3 == b"tfhd":
                    track = struct.unpack(">I", out[c3 + 4:c3 + 8])[0]
                elif k3 == b"tfdt":
                    tfdt = s3
            if track is None or tfdt is None:
                continue
            version = out[tfdt + 8]
            pos = tfdt + 12
            cur = struct.unpack(">Q", out[pos:pos + 8])[0] if version == 1 else struct.unpack(">I", out[pos:pos + 4])[0]
            first = offsets.setdefault(track, cur)
            new = max(0, cur - first)
            if version == 1:
                out[pos:pos + 8] = struct.pack(">Q", new)
            else:
                out[pos:pos + 4] = struct.pack(">I", new & 0xFFFFFFFF)
    return bytes(out)


def prepend_prft(piece: bytes, started_at_ms: float) -> bytes:
    """A 'prft' box (version 1: NTP wall clock + the first track's decode time) before the piece's moof."""
    track, media_time = None, None
    for kind, _s, c, e in _children(piece, 0, len(piece)):
        if kind != b"moof" or track is not None:
            continue
        for k2, _s2, c2, e2 in _children(piece, c, e):
            if k2 != b"traf" or track is not None:
                continue
            for k3, s3, c3, _e3 in _children(piece, c2, e2):
                if k3 == b"tfhd":
                    track = struct.unpack(">I", piece[c3 + 4:c3 + 8])[0]
                elif k3 == b"tfdt":
                    version = piece[s3 + 8]
                    media_time = (struct.unpack(">Q", piece[s3 + 12:s3 + 20])[0] if version == 1
                                  else struct.unpack(">I", piece[s3 + 12:s3 + 16])[0])
    if track is None or media_time is None:
        return piece
    ms = max(0, int(round(started_at_ms)))
    ntp = ((ms // 1000 + NTP_EPOCH_OFFSET) << 32) | (((ms % 1000) << 32) // 1000)
    prft = struct.pack(">I4sB3xIQQ", 32, b"prft", 1, track, ntp, media_time)
    return prft + piece
