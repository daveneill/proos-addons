"""
The desired-state model.

An Activity is "what state should this room be in" (e.g. Family Room watching
Apple TV). It is a set of Targets. Each Target knows three things:

  1. desired   - a human-readable description of the condition it wants
  2. drive()   - given the current snapshot, what command(s) move toward desired
  3. validate()- given the current snapshot, is the condition met? + why/why-not

Crucially, validate() and drive() receive the WHOLE snapshot, not just their own
entity. That is what lets a Target validate by sibling inference -- the Samsung TV
case, where the display can't confirm its own input so we read the source devices.

This file is pure declaration + logic. It has no idea whether HA is local, cloud,
or mocked. It never sleeps, never retries -- that is the reconciler's job.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable
from .ha_client import HAClient, Snapshot


@dataclass
class Command:
    domain: str
    service: str
    entity_id: str
    data: dict | None = None

    def __str__(self) -> str:
        tail = f" {self.data}" if self.data else ""
        return f"{self.domain}.{self.service} -> {self.entity_id}{tail}"


@dataclass
class Check:
    ok: bool
    detail: str


# A driver looks at the snapshot and returns the commands needed to advance this
# target. It returns [] when the target already looks satisfied, so we never send
# redundant commands (no "turn on a TV that's already on").
Driver = Callable[[Snapshot], list[Command]]

# A validator looks at the whole snapshot and decides whether the condition holds.
Validator = Callable[[Snapshot], Check]


@dataclass
class Target:
    name: str               # logical role, e.g. "display", "source", "audio"
    entity_id: str          # the real HA entity this target governs
    desired: str            # human-readable desired condition
    drive: Driver
    validate: Validator
    # Ordering: this target is not DRIVEN until every target named here has
    # validated. This is AV sequencing -- e.g. don't wake the Apple TV (whose
    # CEC one-touch-play switches the input) until the TV is confirmed on and
    # ready to receive it. Firing both at once is exactly what put a blue screen
    # on the wall: the CEC switch hit a TV that was still booting.
    after: list[str] = field(default_factory=list)
    # If True, this target's failure does NOT fail the activity on its own --
    # it's advisory (e.g. audio routing we can't fully confirm). Kept honest:
    # we still report it.
    advisory: bool = False


@dataclass
class Activity:
    key: str                # e.g. "family_watch_appletv"
    room: str               # "Family Room"
    label: str              # "Watch Apple TV" -- what the user actually taps
    targets: list[Target]
    # Activity-level validator: the single human sentence that answers
    # "is the room actually in this state?" -- built from sibling inference.
    summary: Validator
    # Discrete display route fired once per activation by the controller
    # (e.g. {"tv_remote": "remote.family_room_family_room_tv", "hdmi_code":
    # "KEY_HDMI1"}). None for activities that don't route a display.
    route: dict | None = None
    # The source's `remote.` entity, woken once per activation (unconditional --
    # the source's claimed state is untrustworthy on tvOS 26). None for
    # broadcast/sourceless activities.
    wake_remote: str | None = None
    # Structured wake action fired once per activation: {"domain","service",
    # "entity"}. Carries its own domain/service because the path that wakes each
    # source differs (e.g. Apple TV wakes via media_player.turn_on, not
    # remote.turn_on). None for broadcast/sourceless activities. Preferred over
    # wake_remote; the controller fires this when present.
    wake: dict | None = None

    def entity_ids(self) -> list[str]:
        return [t.entity_id for t in self.targets]
