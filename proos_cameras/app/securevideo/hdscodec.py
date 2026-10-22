"""ProOS Cameras — the HomeKit Data Stream value format (Apple's OPACK-like encoding). Register 970u, Route 2.

Ported from HAP-NodeJS src/lib/datastream/DataStreamParser.ts (Apache-2.0) — tags, widths, the 12-element array and
14-entry dictionary limits, and the back-reference ("compression") rules kept exactly, including which values each
side tracks (the reader tracks every decoded value; the writer tracks only strings, data, sized numbers, floats,
dates and uuids — strings and sized values by value, data by identity). One correction: HAP-NodeJS forgets to
return short data (tags 0x70–0x90) when decoding, and its reader stops the small-number range at 0x2E although its
writer puts 39 at 0x2F (Apple's range is 0x08–0x2F); here both are read correctly.
"""
from __future__ import annotations

import struct
import uuid as _uuid


class HDSError(ValueError):
    pass


class _Wrapped:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return type(self) is type(other) and self.value == other.value

    def __hash__(self):
        return hash((type(self).__name__, self.value))

    def __repr__(self):
        return "%s(%r)" % (type(self).__name__, self.value)


class Int8(_Wrapped):
    pass


class Int16(_Wrapped):
    pass


class Int32(_Wrapped):
    pass


class Int64(_Wrapped):
    pass


class Float32(_Wrapped):
    pass


class Float64(_Wrapped):
    pass


class SecondsSince2001(_Wrapped):
    pass


class UUID(_Wrapped):
    def __init__(self, value: str):
        _uuid.UUID(value)
        super().__init__(value)        # kept as given (HAP-NodeJS compares the text as given)


_TERMINATOR = object()

INVALID, TRUE, FALSE, TERMINATOR, NULL, TUUID, DATE, INT_M1 = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
INT_R0, INT_R39 = 0x08, 0x2F      # 0–39; HAP-NodeJS's table says 0x2E but its writer emits 0x2F for 39
INT8, INT16LE, INT32LE, INT64LE, FLOAT32LE, FLOAT64LE = 0x30, 0x31, 0x32, 0x33, 0x35, 0x36
UTF8_S, UTF8_E, UTF8_L8, UTF8_L16, UTF8_L32, UTF8_L64, UTF8_NUL = 0x40, 0x60, 0x61, 0x62, 0x63, 0x64, 0x6F
DATA_S, DATA_E, DATA_L8, DATA_L16, DATA_L32, DATA_L64, DATA_TERM = 0x70, 0x90, 0x91, 0x92, 0x93, 0x94, 0x9F
COMP_S, COMP_E = 0xA0, 0xCF
ARR_S, ARR_E, ARR_TERM = 0xD0, 0xDE, 0xDF
DICT_S, DICT_E, DICT_TERM = 0xE0, 0xEE, 0xEF


class Reader:
    def __init__(self, data: bytes):
        self.data, self.i, self.tracked = bytes(data), 0, []

    def _need(self, n):
        if self.i + n > len(self.data):
            raise HDSError("HDSDecoder: end of data — needed %d bytes, %d left" % (n, len(self.data) - self.i))

    def _take(self, n):
        self._need(n)
        v = self.data[self.i:self.i + n]
        self.i += n
        return v

    def _track(self, v):
        self.tracked.append(v)
        return v

    def _len(self, fmt, n):
        return struct.unpack("<" + fmt, self._take(n))[0]

    def _until(self, stop):
        j = self.data.find(bytes([stop]), self.i)
        if j < 0:
            raise HDSError("HDSDecoder: reached the end while reading terminated data")
        v = self.data[self.i:j]
        self.i = j + 1
        return v


def decode(r: Reader):
    tag = r._take(1)[0]
    if tag == INVALID:
        raise HDSError("HDSDecoder: zero tag detected on index %d" % r.i)
    if tag == TRUE:
        return r._track(True)
    if tag == FALSE:
        return r._track(False)
    if tag == TERMINATOR:
        return _TERMINATOR
    if tag == NULL:
        return None
    if tag == TUUID:
        return r._track(str(_uuid.UUID(bytes=r._take(16))))
    if tag == DATE:
        return r._track(struct.unpack("<d", r._take(8))[0])
    if tag == INT_M1:
        return r._track(-1)
    if INT_R0 <= tag <= INT_R39:
        return r._track(tag - INT_R0)
    if tag == INT8:
        return r._track(struct.unpack("<b", r._take(1))[0])
    if tag == INT16LE:
        return r._track(struct.unpack("<h", r._take(2))[0])
    if tag == INT32LE:
        return r._track(struct.unpack("<i", r._take(4))[0])
    if tag == INT64LE:
        return r._track(struct.unpack("<q", r._take(8))[0])
    if tag == FLOAT32LE:
        return r._track(struct.unpack("<f", r._take(4))[0])
    if tag == FLOAT64LE:
        return r._track(struct.unpack("<d", r._take(8))[0])
    if UTF8_S <= tag <= UTF8_E:
        return r._track(r._take(tag - UTF8_S).decode("utf-8"))
    if tag in (UTF8_L8, UTF8_L16, UTF8_L32, UTF8_L64):
        n = {UTF8_L8: ("B", 1), UTF8_L16: ("H", 2), UTF8_L32: ("I", 4), UTF8_L64: ("Q", 8)}[tag]
        return r._track(r._take(r._len(*n)).decode("utf-8"))
    if tag == UTF8_NUL:
        return r._track(r._until(0).decode("utf-8"))
    if DATA_S <= tag <= DATA_E:
        return r._track(r._take(tag - DATA_S))
    if tag in (DATA_L8, DATA_L16, DATA_L32, DATA_L64):
        n = {DATA_L8: ("B", 1), DATA_L16: ("H", 2), DATA_L32: ("I", 4), DATA_L64: ("Q", 8)}[tag]
        return r._track(r._take(r._len(*n)))
    if tag == DATA_TERM:
        return r._track(r._until(TERMINATOR))
    if COMP_S <= tag <= COMP_E:
        idx = tag - COMP_S
        if idx >= len(r.tracked):
            raise HDSError("HDSDecoder: back-reference %d out of range (%d values)" % (idx, len(r.tracked)))
        return r.tracked[idx]
    if ARR_S <= tag <= ARR_E:
        return [decode(r) for _ in range(tag - ARR_S)]
    if tag == ARR_TERM:
        out = []
        while True:
            v = decode(r)
            if v is _TERMINATOR:
                return out
            out.append(v)
    if DICT_S <= tag <= DICT_E:
        d = {}
        for _ in range(tag - DICT_S):
            k = decode(r)
            d[k] = decode(r)
        return d
    if tag == DICT_TERM:
        d = {}
        while True:
            k = decode(r)
            if k is _TERMINATOR:
                return d
            d[k] = decode(r)
    raise HDSError("HDSDecoder: unknown tag 0x%02x on index %d" % (tag, r.i))


def decode_bytes(data: bytes):
    return decode(Reader(data))


class Writer:
    def __init__(self):
        self.out = bytearray()
        self.written = []

    def data(self) -> bytes:
        return bytes(self.out)

    def _compress(self, v) -> bool:
        idx = -1
        for i, w in enumerate(self.written):
            if (isinstance(v, (bytes, bytearray)) and w is v) or \
               (not isinstance(v, (bytes, bytearray)) and not isinstance(w, (bytes, bytearray))
                    and type(w) is type(v) and w == v):
                idx = i
                break
        if idx < 0:
            self.written.append(v)
            return False
        if idx <= COMP_E - COMP_S:
            self.out.append(COMP_S + idx)
            return True
        return False

    def tag(self, t):
        self.out.append(t)

    def number(self, n: int):
        if n == -1:
            self.tag(INT_M1)
        elif 0 <= n <= 39:
            self.tag(INT_R0 + n)
        elif -128 <= n <= 127:
            self.sized(Int8(n))
        elif -32768 <= n <= 32767:
            self.sized(Int16(n))
        elif -2147483648 <= n <= 2147483647:
            self.sized(Int32(n))
        elif -(2 ** 53 - 1) <= n <= 2 ** 53 - 1:
            self.sized(Int64(n))
        else:
            raise HDSError("Tried writing unrepresentable number (%r)" % n)

    def sized(self, w: _Wrapped):
        if self._compress(w):
            return
        if isinstance(w, Int8):
            self.out += bytes([INT8]) + struct.pack("<b", w.value)
        elif isinstance(w, Int16):
            self.out += bytes([INT16LE]) + struct.pack("<h", w.value)
        elif isinstance(w, Int32):
            self.out += bytes([INT32LE]) + struct.pack("<i", w.value)
        elif isinstance(w, Int64):
            # as HAP-NodeJS: the low 32 bits, then zero (its "TODO correctly implement int64")
            self.out += bytes([INT64LE]) + struct.pack("<I", w.value & 0xFFFFFFFF) + b"\x00\x00\x00\x00"
        elif isinstance(w, Float32):
            self.out += bytes([FLOAT32LE]) + struct.pack("<f", w.value)
        elif isinstance(w, Float64):
            self.out += bytes([FLOAT64LE]) + struct.pack("<d", w.value)
        elif isinstance(w, SecondsSince2001):
            self.out += bytes([DATE]) + struct.pack("<d", w.value)
        elif isinstance(w, UUID):
            self.out += bytes([TUUID]) + bytes.fromhex(w.value.replace("-", ""))
        else:
            raise HDSError("unknown wrapped value %r" % (w,))

    def utf8(self, s: str):
        if self._compress(s):
            return
        b = s.encode("utf-8")
        n = len(b)
        if n <= 32:
            self.out += bytes([UTF8_S + n]) + b
        elif n <= 255:
            self.out += bytes([UTF8_L8, n]) + b
        elif n <= 65535:
            self.out += bytes([UTF8_L16]) + struct.pack("<H", n) + b
        elif n <= 4294967295:
            self.out += bytes([UTF8_L32]) + struct.pack("<I", n) + b
        else:
            self.out += bytes([UTF8_L64]) + struct.pack("<Q", n) + b

    def blob(self, b: bytes):
        if self._compress(b):
            return
        n = len(b)
        if n <= 32:
            self.out += bytes([DATA_S + n]) + b
        elif n <= 255:
            self.out += bytes([DATA_L8, n]) + b
        elif n <= 65535:
            self.out += bytes([DATA_L16]) + struct.pack("<H", n) + b
        elif n <= 4294967295:
            self.out += bytes([DATA_L32]) + struct.pack("<I", n) + b
        else:
            self.out += bytes([DATA_L64]) + struct.pack("<Q", n) + b


def encode(v, w: Writer):
    if v is None:
        w.tag(NULL)
    elif isinstance(v, bool):
        w.tag(TRUE if v else FALSE)
    elif isinstance(v, int):
        w.number(v)
    elif isinstance(v, float):
        w.sized(Float64(v))
    elif isinstance(v, str):
        w.utf8(v)
    elif isinstance(v, _Wrapped):
        w.sized(v)
    elif isinstance(v, (bytes, bytearray)):
        w.blob(v)
    elif isinstance(v, (list, tuple)):
        n = len(v)
        w.tag(ARR_S + n if n <= 12 else ARR_TERM)
        for e in v:
            encode(e, w)
        if n > 12:
            w.tag(TERMINATOR)
    elif isinstance(v, dict):
        items = [(k, x) for k, x in v.items() if x is not _UNDEFINED]
        n = len(items)
        w.tag(DICT_S + n if n <= 14 else DICT_TERM)
        for k, x in items:
            encode(k, w)
            encode(x, w)
        if n > 14:
            w.tag(TERMINATOR)
    else:
        raise HDSError("HDSEncoder: no idea how to encode %r" % (v,))


_UNDEFINED = object()      # a dict value equal to this is left out (HAP-NodeJS drops undefined entries)


def encode_bytes(v) -> bytes:
    w = Writer()
    encode(v, w)
    return w.data()
