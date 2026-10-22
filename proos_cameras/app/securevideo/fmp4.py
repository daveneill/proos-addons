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
