"""
ProOS Core -- broadcast channels.

A channel is NOT an app. An app is launched on a device and carries its own
identity (a package id) that means the same thing everywhere. A channel is
TUNED on one particular source, and its identity is a number that only means
anything on that tuner: 7 on a TV's own tuner, something else entirely on a
set-top box, different again in another region.

So the rules here are deliberately unlike appctl's:

  * A channel belongs to a TUNER, never to a room or a home. Stored against the
    tuner's device_id -- immutable, per the Identity Architecture Standard --
    so renaming anything can never break tuning.
  * Nothing is exposed to a homeowner until an installer COMMITS it. Presets
    are a starting point an installer prunes and confirms, not a list ProOS
    asserts is correct. A channel offered that the customer doesn't receive is
    the same failure as launching an app that isn't installed.
  * Artwork is shared with apps. One tile catalogue, keyed by slug -- nothing
    in the artwork layer knows or cares whether a tile is a channel or an app.

Presets carry METRO numbering and are marked unverified until an installer
confirms them on site: regional affiliates renumber, and networks move
channels. `verified` flips to True on commit, and that flag is what the UI
shows -- ProOS never claims a number is right on its own authority.
"""
from __future__ import annotations

import json
import os
import re

_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "channels.json")
_PRESETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "channel_presets.json")

# How a tuner is told to change channel.
#   play_media  -- media_player.play_media, media_content_type=channel  (TVs,
#                  Android TV boxes; the clean path when the driver supports it)
#   digits      -- send the number as individual key presses on a remote
#                  (set-top boxes with no channel API)
METHODS = ("play_media", "digits")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def presets() -> dict:
    """Starting points, by platform. Shipped with the product, edited centrally."""
    try:
        with open(_PRESETS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return {k: v for k, v in d.items() if not k.startswith("_")}
    except Exception:
        return {}


def _load() -> dict:
    try:
        with open(_STORE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STORE), exist_ok=True)
        tmp = _STORE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh, indent=1, sort_keys=True)
        os.replace(tmp, _STORE)
    except Exception as e:                                       # noqa: BLE001
        print("  [channels] save failed: %s" % e, flush=True)


def for_device(device_id: str) -> dict:
    """What this tuner is committed to carry. Empty until an installer commits."""
    rec = _load().get(device_id) or {}
    return {"device_id": device_id,
            "entity_id": rec.get("entity_id") or "",
            "method": rec.get("method") or "play_media",
            "preset": rec.get("preset") or "",
            "committed": bool(rec.get("committed")),
            "channels": rec.get("channels") or []}


def all_devices() -> dict:
    return {k: for_device(k) for k in _load()}


def _clean(channels) -> list:
    """Keep only rows that could actually tune something."""
    out, seen = [], set()
    for c in (channels or []):
        num = str((c or {}).get("num") or "").strip()
        name = str((c or {}).get("name") or "").strip()
        if not num or not name or num in seen:
            continue
        seen.add(num)
        out.append({"num": num, "name": name,
                    "slug": slug((c or {}).get("slug") or name)})
    return out


def commit(device_id: str, entity_id: str, channels, method: str = "play_media",
           preset: str = "") -> dict:
    """Installer confirms this tuner's line-up. Only now can a homeowner see it."""
    if not device_id:
        return {"error": "which tuner?"}
    if not entity_id:
        return {"error": "that tuner has no media player to tune with"}
    if method not in METHODS:
        return {"error": "unknown tuning method '%s'" % method}
    rows = _clean(channels)
    d = _load()
    if not rows:
        d.pop(device_id, None)                # committing nothing = remove it
        _save(d)
        return {"ok": True, "device_id": device_id, "count": 0,
                "note": "no channels — this tuner shows none"}
    d[device_id] = {"entity_id": entity_id, "method": method, "preset": preset,
                    "committed": True, "channels": rows}
    _save(d)
    print("  [channels] %s: %d committed on %s" % (device_id, len(rows), entity_id),
          flush=True)
    return {"ok": True, "device_id": device_id, "count": len(rows)}


def tune(client, device_id: str, num: str) -> dict:
    """Change to a channel — only one that was committed for THIS tuner.

    Refusing anything not on the committed list is the point: it makes it
    impossible to tune a number nobody confirmed the customer receives."""
    rec = for_device(device_id)
    if not rec["committed"]:
        return {"error": "no channels are set up for this device"}
    num = str(num or "").strip()
    row = next((c for c in rec["channels"] if c["num"] == num), None)
    if not row:
        return {"error": "channel %s isn't set up on this device" % num}
    eid = rec["entity_id"]
    try:
        if rec["method"] == "digits":
            # No channel API: type the number on the remote, then confirm.
            for ch in num:
                client.call_service("remote", "send_command",
                                    {"entity_id": eid.replace("media_player.", "remote."),
                                     "command": "DIGIT_" + ch})
        else:
            client.call_service("media_player", "play_media",
                                {"entity_id": eid, "media_content_type": "channel",
                                 "media_content_id": num})
    except Exception as e:                                       # noqa: BLE001
        return {"error": "couldn't tune %s (%s)" % (row["name"], e)}
    return {"ok": True, "channel": row["name"], "num": num}
