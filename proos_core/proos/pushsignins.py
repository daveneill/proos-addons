"""EVERY APP SIGNED IN TO THIS BOX, AS THE PLATFORM HOLDS IT — register 1354 (15 Nov 2026).

Dave 13:22 "It has to not be a work around", 13:25 "yes". Apple's Delivery Log (15 Nov 12:45) said the test to the
iPhone 17 Pro Max was "discarded as device was offline" one millisecond after Apple received it: the box sent to a
sign-in no phone is listening on. The platform addresses a phone BY ITS NAME (notify.mobile_app_<name>, one channel
per name); since iOS 16 every iPhone signs in as "iPhone" and every iPad as "iPad", so several sign-ins share a name
and only one of them is the channel. ProOS uses the platform's mechanism; it does not build a second alert channel.
This module only READS what the platform holds and what ProOS measured, so Pro can show it and a tech can act:

  · every mobile_app sign-in: its name, which app it is (from the app's own bundle id), the device's model and system
  · when the app last sent the box anything — its own "Last Update Trigger" sensor's last report (the app sends it
    with every update); None when that sensor has nothing since the box started (said so, never guessed)
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
    by_entry = {}
    for e in entity_registry or []:
        eid, ce = e.get("entity_id") or "", e.get("config_entry_id")
        # the platform numbers a second sign-in's sensors (…_last_update_trigger_2) — matched by its unique id too
        if ce and eid.startswith("sensor.") and (re.search(r"_last_update_trigger(_\d+)?$", eid)
                                                 or str(e.get("unique_id") or "").endswith("last_update_trigger")):
            by_entry[ce] = eid
    out = []
    for e in entries or []:
        if e.get("domain") != "mobile_app":
            continue
        d = e.get("data") or {}
        name = d.get("device_name") or e.get("title") or "a device"
        heard = None
        trig = by_entry.get(e.get("entry_id"))
        if trig:
            s = state_of(trig) or {}
            if s.get("state") not in NOT_HEARD:
                heard = s.get("last_reported") or s.get("last_updated")
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
