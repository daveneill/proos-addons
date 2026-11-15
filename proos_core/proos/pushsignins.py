"""EVERY APP SIGNED IN TO THIS BOX, AS THE PLATFORM HOLDS IT — register 1354 (15 Nov 2026).

Dave 13:22 "It has to not be a work around", 13:25 "yes". Apple's Delivery Log (15 Nov 12:45) said the test to the
iPhone 17 Pro Max was "discarded as device was offline" one millisecond after Apple received it: the box sent to a
sign-in no phone is listening on. The platform addresses a phone BY ITS NAME (notify.mobile_app_<name>, one channel
per name); since iOS 16 every iPhone signs in as "iPhone" and every iPad as "iPad", so several sign-ins share a name
and only one of them is the channel. ProOS uses the platform's mechanism; it does not build a second alert channel.
This module only READS what the platform holds and what ProOS measured, so Pro can show it and a tech can act:

  · every mobile_app sign-in: its name, which app it is (from the app's own bundle id), the device's model and system
  · when the app last reported to the box — the platform's last_reported stamp on the app's own readings, after the
    box last started; None when it has not reported since (said so, never guessed)
  · which sign-ins share an alert channel, and — measured, from the box's own push relay — which sign-in the last alert
    for that channel actually went to

Nothing secret leaves here: no push token, no webhook id.
"""
from __future__ import annotations

import re
import unicodedata

PREFIX = "au.com.protechsystems.ProOS"
# which app, by the app's own bundle id (the app names itself at sign-in; nothing inferred)
APPS = (("au.com.protechsystems.ProOSPro.dev", "Pro · developer build"),
        ("au.com.protechsystems.ProOSPro", "Pro"),
        ("au.com.protechsystems.ProOS.dev", "Homeowner · developer build"),
        ("au.com.protechsystems.ProOS", "Homeowner"))
NOT_HEARD = ("unavailable", "unknown", "", None)


def slug(text):
    """The platform's own slug for a name (notify.mobile_app_<slug>) — the same rule as server._slug_ha."""
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    t = re.sub(r"['’]", "", t.lower())
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")


def _after(t, start, slack_s=5):
    """t is later than the platform's start instant (plus a few seconds' plumbing: the restore takes a moment)."""
    from datetime import datetime
    try:
        f = lambda x: datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
        return not start or f(t) > f(start) + slack_s
    except Exception:  # noqa: BLE001
        return False


def app_kind(app_id):
    a = str(app_id or "")
    for bid, words in APPS:
        if a == bid:
            return words
    return "Another app (%s)" % a if a else "Another app"


def signins(entries, entity_registry, state_of, push_status):
    """entries: the platform's config entries (storage shape: entry_id, domain, data, created_at).
    entity_registry: [{entity_id, config_entry_id}]. state_of(entity_id) → {state, last_reported} or None.
    push_status: {device_name: {entry_id?, ok, words, at, receipt}} — the box's own record of each alert.
    Returns the list Pro shows, oldest sign-in first."""
    # REGISTER 1354, READ ON DAVE'S BOX: most app versions never send a Last Update Trigger sensor (2 of 8 sign-ins had
    # one, both unavailable), so "last heard" is read from what EVERY app sends: its readings (battery, app version,
    # location permission…), which the platform stamps last_reported each time the app reports — even unchanged. The
    # box's own writes (notify.* entities, stamped when an alert is sent) are not the app. At start the platform
    # restores every reading with the start instant; that instant is not a report, so only a stamp after it counts.
    mine = {e.get("entry_id") for e in entries or [] if e.get("domain") == "mobile_app"}
    by_entry, stamps = {}, []
    for e in entity_registry or []:
        eid, ce = e.get("entity_id") or "", e.get("config_entry_id")
        if ce not in mine or eid.startswith("notify."):
            continue
        s_ = state_of(eid) or {}
        lu = s_.get("last_updated")
        if lu:
            stamps.append(lu)
        if s_.get("state") in NOT_HEARD:
            continue
        lr = s_.get("last_reported") or lu
        if lr:
            by_entry.setdefault(ce, []).append(lr)
    start = min(stamps) if stamps else ""
    out = []
    for e in entries or []:
        if e.get("domain") != "mobile_app":
            continue
        d = e.get("data") or {}
        name = d.get("device_name") or e.get("title") or "a device"
        after = [t for t in by_entry.get(e.get("entry_id"), []) if _after(t, start)]
        heard = max(after) if after else None
        out.append({"entry_id": e.get("entry_id"), "name": name, "channel": "mobile_app_" + slug(name),
                    "app": app_kind(d.get("app_id")), "proos": str(d.get("app_id") or "").startswith(PREFIX),
                    "model": d.get("model") or "", "os": " ".join(x for x in (d.get("os_name"), d.get("os_version")) if x),
                    "app_version": d.get("app_version") or "", "created": e.get("created_at") or "",
                    "heard": heard})
    count = {}
    for r in out:
        count[r["channel"]] = count.get(r["channel"], 0) + 1
    for r in out:
        r["shares"] = count[r["channel"]] - 1
        last = (push_status or {}).get(r["name"]) or {}
        r["last_alert_here"] = bool(last.get("entry_id")) and last.get("entry_id") == r["entry_id"]
        r["last_alert"] = {k: last.get(k) for k in ("ok", "words", "at", "receipt")} if r["last_alert_here"] else None
    out.sort(key=lambda r: str(r["created"]))
    return out
