"""HEALTH'S OPEN FAULTS → THE PLATFORM'S REPAIRS LIST (register 1326, 12 Nov 2026 — plan item D18).

Only an integration can raise a repair on the platform (the 5 Oct finding); ProOS's door is that integration (door
build 10, repairs.py). Core sends the door Health's whole open list on the platform's event bus ("proos_incidents"),
whenever the list changes and every PUSH_EVERY seconds regardless (a door that restarted catches up), and the door
makes the platform's ProOS repairs exactly that list. Health stays where the fixes are; each repair says so.

What is NOT sent: Health's cards that came FROM the platform's own fault lists (platformfaults: failing integrations,
Repairs, the Supervisor's issues — kinds "platform_*"). Sent back they would be the platform's fault twice on its own
list, and Health would read them back again. The reader skips ProOS's own repairs for the same reason
(platformfaults: domain "proos").
"""
from __future__ import annotations

import hashlib
import json
import time

EVENT = "proos_incidents"
PUSH_EVERY = 300          # plumbing: the whole list again, so a door that restarted is never left out of date


def payload(incidents) -> list:
    out = []
    for i in incidents or []:
        if not isinstance(i, dict) or not i.get("id"):
            continue
        if str(i.get("kind") or "").startswith("platform_"):
            continue
        acts = [a.get("label") for a in (i.get("actions") or []) if isinstance(a, dict) and a.get("label")]
        out.append({"id": str(i["id"]), "title": str(i.get("title") or "A ProOS fault"),
                    "cause": str(i.get("cause") or ""),
                    "severity": str(i.get("severity") or "warning"),
                    "fix": ("In ProOS Pro, Health: %s." % " or ".join(acts)) if acts
                    else "Open ProOS Pro, Health, for what to do."})
    return sorted(out, key=lambda x: x["id"])


class Pusher:
    def __init__(self):
        self._digest = None
        self._at = 0.0

    def tick(self, client, incidents, now=None) -> bool:
        """Send the list when it changed or PUSH_EVERY has passed. True when sent."""
        now = float(now or time.time())
        items = payload(incidents)
        d = hashlib.sha1(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()
        if d == self._digest and now - self._at < PUSH_EVERY:
            return False
        client._req("POST", "/api/events/" + EVENT, {"incidents": items})
        self._digest, self._at = d, now
        return True
