"""HTS binary protocol framing: STX/ETX, escape, CRC-16/CCITT."""

import os
import struct

STX = 0x02
ETX = 0x03
ESC = 0x04

_ESCAPE_MAP = {
    STX: bytes([ESC, 0x32]),
    ETX: bytes([ESC, 0x33]),
    ESC: bytes([ESC, 0x34]),
}

_UNESCAPE_MAP = {
    0x32: STX,
    0x33: ETX,
    0x34: ESC,
}

# CRC-16/CCITT lookup table (poly 0x1021, init 0xA001)
# CRC-16/CCITT lookup table (polynomial 0x1021)
_CRC_TABLE = [
    0,
    4129,
    8258,
    12387,
    16516,
    20645,
    24774,
    28903,
    33032,
    37161,
    41290,
    45419,
    49548,
    53677,
    57806,
    61935,
    4657,
    528,
    12915,
    8786,
    21173,
    17044,
    29431,
    25302,
    37689,
    33560,
    45947,
    41818,
    54205,
    50076,
    62463,
    58334,
    9314,
    13379,
    1056,
    5121,
    25830,
    29895,
    17572,
    21637,
    42346,
    46411,
    34088,
    38153,
    58862,
    62927,
    50604,
    54669,
    13907,
    9842,
    5649,
    1584,
    30423,
    26358,
    22165,
    18100,
    46939,
    42874,
    38681,
    34616,
    63455,
    59390,
    55197,
    51132,
    18628,
    22757,
    26758,
    30887,
    2112,
    6241,
    10242,
    14371,
    51660,
    55789,
    59790,
    63919,
    35144,
    39273,
    43274,
    47403,
    23285,
    19156,
    31415,
    27286,
    6769,
    2640,
    14899,
    10770,
    56317,
    52188,
    64447,
    60318,
    39801,
    35672,
    47931,
    43802,
    27814,
    31879,
    19684,
    23749,
    11298,
    15363,
    3168,
    7233,
    60846,
    64911,
    52716,
    56781,
    44330,
    48395,
    36200,
    40265,
    32407,
    28342,
    24277,
    20212,
    15891,
    11826,
    7761,
    3696,
    65439,
    61374,
    57309,
    53244,
    48923,
    44858,
    40793,
    36728,
    37256,
    33193,
    45514,
    41451,
    53516,
    49453,
    61774,
    57711,
    4224,
    161,
    12482,
    8419,
    20484,
    16421,
    28742,
    24679,
    33721,
    37784,
    41979,
    46042,
    49981,
    54044,
    58239,
    62302,
    689,
    4752,
    8947,
    13010,
    16949,
    21012,
    25207,
    29270,
    46570,
    42443,
    38312,
    34185,
    62830,
    58703,
    54572,
    50445,
    13538,
    9411,
    5280,
    1153,
    29798,
    25671,
    21540,
    17413,
    42971,
    47098,
    34713,
    38840,
    59231,
    63358,
    50973,
    55100,
    9939,
    14066,
    1681,
    5808,
    26199,
    30326,
    17941,
    22068,
    55628,
    51565,
    63758,
    59695,
    39368,
    35305,
    47498,
    43435,
    22596,
    18533,
    30726,
    26663,
    6336,
    2273,
    14466,
    10403,
    52093,
    56156,
    60223,
    64286,
    35833,
    39896,
    43963,
    48026,
    19061,
    23124,
    27191,
    31254,
    2801,
    6864,
    10931,
    14994,
    64814,
    60687,
    56684,
    52557,
    48554,
    44427,
    40424,
    36297,
    31782,
    27655,
    23652,
    19525,
    15522,
    11395,
    7392,
    3265,
    61215,
    65342,
    53085,
    57212,
    44955,
    49082,
    36825,
    40952,
    28183,
    32310,
    20053,
    24180,
    11923,
    16050,
    3793,
    7920,
]


def crc16(data: bytes) -> int:
    """Compute CRC-16/CCITT checksum."""
    crc = 0xA001
    for byte in data:
        crc = (_CRC_TABLE[((crc >> 8) & 0xFF) ^ (byte & 0xFF)] ^ (crc << 8)) & 0xFFFF
    return crc


def escape(data: bytes) -> bytes:
    """Escape STX, ETX, and ESC bytes for framing."""
    out = bytearray()
    for byte in data:
        replacement = _ESCAPE_MAP.get(byte)
        if replacement is not None:
            out.extend(replacement)
        else:
            out.append(byte)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    """Unescape a previously escaped byte sequence."""
    out = bytearray()
    i = 0
    while i < len(data):
        byte = data[i]
        if byte == ESC:
            if i + 1 >= len(data):
                raise ValueError("Trailing ESC byte with no following byte")
            suffix = data[i + 1]
            if suffix not in _UNESCAPE_MAP:
                raise ValueError(f"Invalid escape sequence: ESC 0x{suffix:02X}")
            out.append(_UNESCAPE_MAP[suffix])
            i += 2
        else:
            out.append(byte)
            i += 1
    return bytes(out)


def pad16(data: bytes) -> bytes:
    """Pad *data* to a multiple of 16 bytes with random bytes (each >= 10).

    If already aligned, returns data unchanged.
    """
    remainder = len(data) % 16
    if remainder == 0:
        return data
    pad_len = 16 - remainder
    padding = bytes(max(b, 10) for b in os.urandom(pad_len))
    return data + padding


def encode_frame(encrypted_body: bytes) -> bytes:
    """Wrap *encrypted_body* in an HTS frame: STX + escaped(body + CRC BE) + ETX."""
    checksum = crc16(encrypted_body)
    crc_bytes = struct.pack(">H", checksum)
    payload = encrypted_body + crc_bytes
    return bytes([STX]) + escape(payload) + bytes([ETX])


def decode_frame(frame: bytes) -> bytes:
    """Decode an HTS frame, verify CRC, and return the body.

    Raises ValueError for malformed or corrupt frames.
    """
    if len(frame) < 2:
        raise ValueError("Frame too short")
    if frame[0] != STX:
        raise ValueError(f"Missing STX: got 0x{frame[0]:02X}")
    if frame[-1] != ETX:
        raise ValueError(f"Missing ETX: got 0x{frame[-1]:02X}")

    inner = frame[1:-1]
    payload = unescape(inner)

    if len(payload) < 2:
        raise ValueError("Frame payload too short to contain CRC")

    body = payload[:-2]
    received_crc = struct.unpack(">H", payload[-2:])[0]
    computed_crc = crc16(body)

    if received_crc != computed_crc:
        raise ValueError(f"CRC mismatch: expected 0x{computed_crc:04X}, got 0x{received_crc:04X}")

    return body
