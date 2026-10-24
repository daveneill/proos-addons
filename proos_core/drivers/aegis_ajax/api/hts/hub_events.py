"""Parser for hub-sourced `type=0x08` event frames on the HTS stream (#454).

Besides the *space* events (`params[0]=0x02`, arm/disarm/chime — see
`client._is_space_event`), the hub emits events about itself with the shape

    params[0]=0x0b, params[1]=0x21, params[2]=<4-byte HUB id>, params[3]=<code>,
    then keyed values, each its own TLV param prefixed `fd fd <key>`:
        0x06  event sequence (monotonic counter)
        0x07  hub-clock unix timestamp (seconds) of the event
        0x17  hub-clock unix timestamp the running delay EXPIRES (code 0x74 only)
        0x1a / 0x04  small 2-byte values, meaning not pinned yet
    followed by a short trailing param without a marker (ignored).

Two codes are pinned from real captures (bvis-home 2026-09-05, dheuts90 #284
2026-06-12), both documented in `docs/protocol-notes/`:

* `0x68` **exit delay complete** — emitted once the longest per-detector
  "Delay when leaving" has run out after an arm (19 s after a 20 s arm on
  bvis-home; 29 s on dheuts90's ~30 s night arm). NOT emitted when the space
  is disarmed before the delay runs out.
* `0x74` **entry delay started** — emitted when a detector with a "Delay when
  entering" triggers while armed; `0x17 - 0x07` equals that detector's
  `alarm_delay_seconds` exactly (20 s observed).

Everything here is pure parsing on already-TLV-decoded params — the client
gates on `is_hub_event` and forwards a `HubEvent` to the coordinator, which
owns the panel semantics. Unknown codes still parse, so the coordinator can
log them at DEBUG for the next capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_HUB_EVENT_FIRST: bytes = b"\x0b"
_HUB_EVENT_SECOND: bytes = b"\x21"
_KEYED_VALUE_MARKER: bytes = b"\xfd\xfd"

KEY_EVENT_TIMESTAMP = 0x07
KEY_DELAY_EXPIRES_AT = 0x17

HUB_EVENT_EXIT_DELAY_COMPLETE = 0x68
HUB_EVENT_ENTRY_DELAY_STARTED = 0x74


@dataclass(frozen=True)
class HubEvent:
    """One decoded hub-sourced event frame."""

    hub_id: str
    """Upper-case 8-hex-char hub id, same shape as `coordinator.hub_network` keys."""
    code: int
    hub_ts: int | None = None
    """Hub-clock unix seconds of the event (key 0x07), when present and non-empty."""
    expires_at: int | None = None
    """Hub-clock unix seconds the delay expires (key 0x17), 0x74 only."""
    values: dict[int, bytes] = field(default_factory=dict)
    """All `fdfd`-keyed values, raw, for DEBUG logging / future keys."""

    @property
    def delay_seconds(self) -> int | None:
        """Length of the delay the event announces, from the hub's own clock.

        Both stamps come from the same clock, so their difference is immune
        to hub/HA clock skew — which is why the coordinator uses this rather
        than comparing `expires_at` with local time.
        """
        if self.hub_ts is None or self.expires_at is None:
            return None
        return self.expires_at - self.hub_ts


def is_hub_event(params: list[bytes]) -> bool:
    """Recognise the hub-event shape: 0x0b, 0x21, 4-byte hub id, 1-byte code."""
    return (
        len(params) >= 4
        and params[0] == _HUB_EVENT_FIRST
        and params[1] == _HUB_EVENT_SECOND
        and len(params[2]) == 4
        and len(params[3]) == 1
    )


def _to_timestamp(value: bytes) -> int | None:
    """Decode a unix-seconds value (observed as a leading 0x00 pad + 4 bytes).

    Anything shorter than 4 bytes cannot be a timestamp — dheuts90's frame
    carries a `00` stub where the log's text redaction ate the real value.
    """
    if len(value) < 4 or len(value) > 8:
        return None
    return int.from_bytes(value, "big")


def parse_hub_event(params: list[bytes]) -> HubEvent | None:
    """Decode a hub event, or None when `params` is not one."""
    if not is_hub_event(params):
        return None
    values: dict[int, bytes] = {}
    for param in params[4:]:
        if len(param) < 3 or not param.startswith(_KEYED_VALUE_MARKER):
            continue
        values[param[2]] = param[3:]
    return HubEvent(
        hub_id=params[2].hex().upper(),
        code=params[3][0],
        hub_ts=_to_timestamp(values.get(KEY_EVENT_TIMESTAMP, b"")),
        expires_at=_to_timestamp(values.get(KEY_DELAY_EXPIRES_AT, b"")),
        values=values,
    )
