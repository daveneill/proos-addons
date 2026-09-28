"""
HA access contract.

Core never talks to Home Assistant directly. It talks to an HAClient.
The SAME engine runs against:
  - MockHA            (deterministic, for the PoC + CI)
  - LocalHA           (WebSocket to localhost:8123 when ProOS is an HA add-on)
  - RemoteHA          (WebSocket to Nabu Casa / tunnel when ProOS is cloud-hosted)

Add-on vs cloud is ONLY a difference in which endpoint this client points at.
The engine above it is identical. That is the whole point of this seam.
"""
from __future__ import annotations
from typing import Protocol


# A snapshot is {entity_id: {"state": str, "attributes": dict}}
Snapshot = dict[str, dict]


class HAClient(Protocol):
    home_id: str

    def snapshot(self, entity_ids: list[str]) -> Snapshot:
        """Read current state of the given entities. One round trip."""
        ...

    def call_service(self, domain: str, service: str, entity_id: str, data: dict | None = None) -> None:
        """Fire-and-forget command. Validation is the reconciler's job, never the command's."""
        ...
