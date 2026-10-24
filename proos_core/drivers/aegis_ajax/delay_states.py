"""Exit / entry delays shown as `arming` / `pending` panel states (#454).

Pure data for the coordinator's delay overlay. Ajax runs the per-detector
"Delay when leaving" / "Delay when entering" inside the hub: the served
`SecurityState` flips straight to ARMED, and the only wire signals are the
hub's HTS events (`api/hts/hub_events.py`) plus the per-detector seconds in
its SETTINGS_BODY rows:

    0xAC  arm_delay_seconds    (2 bytes, big-endian) — "Delay when leaving"
    0xAD  alarm_delay_seconds  (2 bytes, big-endian) — "Delay when entering"
    0xAE  apply the delays in night mode too (1 byte, 00/01)

The overlay is *bounded by construction*: every entry carries a deadline
(the hub's own expiry when it gives one, the configured seconds otherwise),
so a missed hub frame can at worst leave the state a couple of seconds long,
never stuck. Nothing here is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

KEY_ARM_DELAY_SECONDS = 0xAC
KEY_ALARM_DELAY_SECONDS = 0xAD
KEY_DELAYS_IN_NIGHT_MODE = 0xAE

# Slack added on top of the configured / announced seconds before the overlay
# self-clears without the hub's word. Covers HTS delivery latency (the 0x68
# frame arrived ~1 s after the delay on bvis-home) without visibly overstaying.
DELAY_OVERLAY_GRACE_SECONDS = 2


class DelayKind(StrEnum):
    """Which delay the overlay is showing."""

    ARMING = "arming"
    """Exit delay running: armed, exit-route detectors not yet live."""
    PENDING = "pending"
    """Entry delay running: a delayed detector triggered, alarm not yet raised."""


@dataclass(frozen=True)
class ArmDelays:
    """Per-detector delay settings from its SETTINGS_BODY row."""

    arm_delay_seconds: int
    alarm_delay_seconds: int
    night_mode: bool | None
    """Whether the delays apply in night mode; None when 0xAE was not in the row."""


@dataclass(frozen=True)
class DelayOverlay:
    """One running delay for a space. Held in memory only."""

    kind: DelayKind
    ends_at: datetime
    """Expected end, on HA's clock (the panel's `delay_ends_at` attribute)."""
    from_hub: bool
    """True when the end came from the hub's own expiry stamp, False when
    it is the configured seconds serving as the fallback bound."""


def _u16(value: bytes | None) -> int:
    if not value:
        return 0
    return int.from_bytes(value[:2], "big")


def parse_arm_delays(kv: dict[int, bytes]) -> ArmDelays | None:
    """Read the delay settings from a device kv row, or None when absent.

    Absent means the row carries neither 0xAC nor 0xAD — the 60 s STATUS_BODY
    rows never do — so the caller keeps the value the SETTINGS_BODY gave it.
    """
    if KEY_ARM_DELAY_SECONDS not in kv and KEY_ALARM_DELAY_SECONDS not in kv:
        return None
    night = kv.get(KEY_DELAYS_IN_NIGHT_MODE)
    return ArmDelays(
        arm_delay_seconds=_u16(kv.get(KEY_ARM_DELAY_SECONDS)),
        alarm_delay_seconds=_u16(kv.get(KEY_ALARM_DELAY_SECONDS)),
        night_mode=None if not night else bool(night[0]),
    )
