"""
ProOS Core -- the proactive Pro.

A Pro who only speaks when spoken to isn't a Pro. This loop watches the same
verdicts the dashboards render and TELLS the household, in plain language,
when something needs them -- and, just as importantly, when it's been handled.

Design rules:
  * VERDICTS ONLY. A notification is sent when the Watcher has confirmed a
    fault (its own pending->fault debounce has already run). Nothing here
    re-diagnoses; this is a messenger for the awareness layer, not a second
    opinion.
  * ONE notice per fault episode, and a close-out when it recovers. A device
    that flaps doesn't spam: an episode re-notifies only after the cooldown.
  * PLAIN LANGUAGE, offline. Messages are built from the watcher's own
    guidance text -- no model call, no API key, no internet dependency. The
    assistant is for conversation; telling someone their TV is unreachable
    must work with the WAN down.
  * QUIET HOURS. Nothing non-urgent between 22:00 and 07:00 local; it queues
    and arrives in the morning summary instead. A fault that RECOVERS while
    queued is dropped entirely -- nobody needs yesterday's fixed problems.
  * Delivery is HA's own notify.mobile_app_* services, so it reaches every
    phone signed into the home's app with zero token bookkeeping here.

State lives in /data/proactive.json so a Core restart doesn't re-announce
every standing fault.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

_STATE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "proactive.json")
_INTERVAL = 60          # seconds between sweeps
_COOLDOWN = 6 * 3600    # re-notify a STILL-faulted device at most this often
_NOPATH_HOLD = 180      # a no-path box waits this long for its switch's card before it is said alone (register 654)
_QUIET_START, _QUIET_END = 22, 7
_BOOT = time.time()


def _now() -> float:
    return time.time()


def _quiet() -> bool:
    h = datetime.now().hour
    return h >= _QUIET_START or h < _QUIET_END


# WHICH HEALTH CARDS REACH A PHONE (plan item 6; register 451's question; Dave's
# ruling R2 OWED — built to the recommendation in the 1 Oct plan §5 and reversible
# here, in one place): a card released to the household (they are living it), and
# any CRITICAL card. Warnings for the installer stay on Pro.
def incident_reaches_phone(inc: dict) -> bool:
    return (inc.get("audience") == "home") or (inc.get("severity") == "critical")


class Proactive:
    def __init__(self, client, watcher, enabled=lambda: True, incidents=None):
        self.client = client
        self.watcher = watcher
        self.enabled = enabled
        # THE HOP TO THE PHONE (plan item 6, G-41): Health incidents used to
        # stop at Pro. `incidents` is healthmon.incidents — the same cards Pro
        # shows; they ride THIS messenger's episode, cooldown and quiet-hours
        # machinery (benched in register 234), so a card is said once, closed
        # out once, never spammed.
        self.incidents = incidents
        self._state = self._load()
        self._lock = threading.Lock()

    # -- state ---------------------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(_STATE, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(_STATE), exist_ok=True)
            tmp = _STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=1)
            os.replace(tmp, _STATE)
        except Exception:
            pass

    # -- delivery ------------------------------------------------------------
    def _notify_services(self) -> list:
        """Every phone signed into the home: HA registers one notify service
        per mobile_app device."""
        out = []
        try:
            for dom in (self.client._req("GET", "/api/services") or []):
                if dom.get("domain") != "notify":
                    continue
                for svc in (dom.get("services") or {}):
                    if svc.startswith("mobile_app"):
                        out.append(svc)
        except Exception:
            pass
        return out

    def _deliver(self, title: str, message: str) -> int:
        sent = 0
        for svc in self._notify_services():
            try:
                self.client._req("POST", "/api/services/notify/%s" % svc,
                                 {"title": title, "message": message})
                sent += 1
            except Exception:
                continue
        if sent:
            print("  [proactive] notified %d device(s): %s" % (sent, title), flush=True)
        return sent

    # -- messages ------------------------------------------------------------
    @staticmethod
    def _fault_text(item: dict) -> tuple:
        if item.get("_incident"):
            return (item.get("_title") or "ProOS needs attention",
                    (item.get("guidance") or "").strip() or "Open ProOS for details.")
        name = item.get("name") or "A device"
        area = item.get("area")
        where = (" in the %s" % area) if area else ""
        guidance = (item.get("guidance") or "").strip()
        body = guidance or ("%s%s isn't responding." % (name, where))
        if item.get("recovery") == "recovering":
            body += " ProOS is trying to bring it back automatically."
        return ("%s needs attention" % name, body)

    @staticmethod
    def _recovered_text(item_name: str, auto: bool) -> tuple:
        if auto:
            return ("%s is back" % item_name,
                    "%s went offline earlier — ProOS restarted it and it's "
                    "working again. Nothing to do." % item_name)
        return ("%s is back" % item_name,
                "%s is responding again and everything looks normal." % item_name)

    def _device_of(self, eid):
        try:
            for e in (self.client.entity_registry() or []):
                if e.get("entity_id") == eid:
                    return e.get("device_id")
        except Exception:                                            # noqa: BLE001
            pass
        return None

    def _incident_items(self, watch_items) -> list:
        """Health cards as messenger items. A card about a device the watcher
        has ALREADY faulted is not said twice (same platform device)."""
        if self.incidents is None:
            return []
        try:
            incs = [i for i in (self.incidents() or []) if incident_reaches_phone(i)]
        except Exception:                                            # noqa: BLE001
            return []
        if not incs:
            return []
        faulted_devs = {self._device_of(w.get("entity")) for w in watch_items
                        if w.get("status") == "fault" and w.get("entity")}
        faulted_devs.discard(None)
        out = []
        for i in incs:
            subj = i.get("subject")
            if subj and "." in str(subj) and self._device_of(subj) in faulted_devs:
                continue
            out.append({"name": "card:%s" % i.get("id"), "status": "fault",
                        "_title": i.get("title"), "guidance": i.get("cause"),
                        "_incident": True, "_kind": i.get("kind"),
                        # a card answering a person's own act just now (it carries
                        # what was fired) is the answer to their press — quiet
                        # hours hold notices nobody is waiting for, not this one
                        "_answers_an_act": bool(i.get("fired"))})
        return out

    def _episodes_since_last(self, items) -> list:
        """FAULTS THAT CAME AND WENT BETWEEN SWEEPS (plan item 7, rig T3h). A
        device ProOS brought back inside a minute was never seen faulted by a
        60-second sweep, so nobody was ever told it happened. The watcher's own
        audit log records every recovery; each one newer than the last sweep
        that this messenger did not already announce is said once, as what
        happened. Nothing is inferred: the log line is the fact."""
        path = getattr(self.watcher, "audit_path", None) if self.watcher else None
        if not path:
            return []
        # first sweep on this box: start from when Core started — history
        # from before this messenger existed is not news; what happened since
        # boot (even before the first sweep) is
        last = float(self._state.get("_audit_at", 0) or 0) or _BOOT
        names = {i.get("entity"): i.get("name") for i in items if i.get("entity")}
        out, newest = [], last
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()[-400:]
        except Exception:                                            # noqa: BLE001
            return []
        from datetime import datetime as _dt
        attempts, fault_why = {}, {}
        for ln in lines:
            parts = ln.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            try:
                ts = _dt.fromisoformat(parts[0].replace("Z", "+00:00")).timestamp()
            except Exception:                                        # noqa: BLE001
                continue
            ent, ev = parts[1], parts[2]
            if ev in ("attempt_done", "attempt_error"):
                attempts[ent] = parts[3] if len(parts) > 3 else ""
            if ev == "fault":
                fault_why[ent] = parts[3] if len(parts) > 3 else ""
            if ts <= last:
                continue
            newest = max(newest, ts)
            if ev == "recovered":
                # a box that was only ever cut off by its switch came back WITH the
                # switch: the switch card's close-out says that, once (register 654)
                if fault_why.get(ent) == "no_path":
                    continue
                out.append((ent, names.get(ent) or ent, attempts.get(ent, ""), ts))
        self._state["_audit_at"] = newest
        return out

    # -- the sweep -----------------------------------------------------------
    def sweep(self) -> dict:
        """One pass. Returns what it did (also used by the test endpoint)."""
        if not self.enabled():
            return {"enabled": False}
        rep = (self.watcher.report() if self.watcher else None) or {}
        items = list(rep.get("items") or [])
        cards = self._incident_items(items)
        # NO PATH IS SAID ONCE, AS THE GEAR (register 654, rig T1d; Claims #7). A box
        # the watcher says has no network path because its own switch is down is not
        # its own phone message while the switch's card is going out — that card
        # names it. Without a gear card in this pass nothing is suppressed.
        # The watcher reads the switch port and can say "no path" up to a health
        # scan BEFORE the gear card exists (rig T1d, second run: the boxes went out
        # 14 s ahead of the card). So a no-path box WAITS for the gear card, up to
        # _NOPATH_HOLD; only if no gear card comes is it said on its own.
        gear_card = any(str(c.get("_kind")) == "infra_down" for c in cards)
        kept = []
        for i in items:
            if not (i.get("status") == "fault" and i.get("verdict") == "no_path"):
                kept.append(i)
                continue
            nm = i.get("name") or ""
            rec = self._state.get(nm) if isinstance(self._state.get(nm), dict) else {}
            if gear_card:
                if rec.get("stage") == "held":
                    self._state.pop(nm, None)
                continue
            if rec.get("stage") in ("notified", "queued"):
                kept.append(i)                     # already in the normal episode
            elif rec.get("stage") != "held":
                self._state[nm] = {"stage": "held", "at": _now()}
            elif _now() - rec.get("at", 0) >= _NOPATH_HOLD:
                kept.append(i)                     # no gear card came: said on its own
        items = kept + cards
        acted = {"notified": [], "recovered": [], "queued": [], "skipped": []}
        with self._lock:
            seen_faults = set()
            for it in items:
                name = it.get("name") or ""
                if not name:
                    continue
                rec = self._state.get(name) or {}
                if not isinstance(rec, dict):
                    rec = {}
                if it.get("status") == "fault":
                    seen_faults.add(name)
                    already = rec.get("stage")
                    if already == "notified" and _now() - rec.get("at", 0) < _COOLDOWN:
                        acted["skipped"].append(name)
                        continue
                    if _quiet() and not it.get("_answers_an_act"):
                        # Queue it: announced in the morning if still faulted.
                        self._state[name] = {"stage": "queued", "at": _now()}
                        acted["queued"].append(name)
                        continue
                    title, body = self._fault_text(it)
                    if self._deliver(title, body):
                        self._state[name] = {"stage": "notified", "at": _now(), "title": title}
                        acted["notified"].append(name)
                else:
                    # Healthy (ok/standby/amber). Close out anything announced.
                    if rec.get("stage") == "notified":
                        auto = (it.get("recovery") == "recovered")
                        title, body = self._recovered_text(name, auto)
                        if not _quiet():
                            self._deliver(title, body)
                        # Either way the episode is over.
                        self._state.pop(name, None)
                        acted["recovered"].append(name)
                    elif rec.get("stage") in ("queued", "held"):
                        # Fixed before anyone was told — say nothing, ever.
                        self._state.pop(name, None)
            # Recoveries that happened between sweeps: said once, as what happened.
            for ent, nm, how, ts in self._episodes_since_last(items):
                rec = self._state.get(nm) or {}
                if rec.get("stage") == "notified" or nm in acted["recovered"]:
                    continue                    # already told; the close-out covers it
                if _quiet():
                    continue                    # fixed overnight: nobody needs yesterday's fixed problems
                when = datetime.fromtimestamp(ts).strftime("%-I:%M %p").lower()
                self._deliver("%s is back" % nm,
                              "%s stopped responding and ProOS brought it back at %s%s. Nothing to do."
                              % (nm, when, (" (%s)" % how.replace("_", " ")) if how and how != "assist" else ""))
                acted["recovered"].append(nm)
            # A card that has CLEARED is absent from this pass: close it out
            # once (it was said), or forget it silently (it never was).
            present = {it.get("name") for it in items}
            for name, rec in list(self._state.items()):
                if not isinstance(rec, dict):
                    continue                    # "_audit_at" and other bookkeeping
                if not str(name).startswith("card:") or name in present:
                    continue
                if rec.get("stage") == "notified" and not _quiet():
                    self._deliver("Resolved: %s" % (rec.get("title") or "a ProOS alert"),
                                  "ProOS no longer sees this. Nothing to do.")
                    acted["recovered"].append(name)
                self._state.pop(name, None)
            # Morning flush: queued faults that are STILL faulted get announced
            # once quiet hours end (they're in seen_faults, stage queued).
            if not _quiet():
                for name, rec in list(self._state.items()):
                    if not isinstance(rec, dict):
                        continue                # "_audit_at" and other bookkeeping
                    if rec.get("stage") == "queued":
                        if name in seen_faults:
                            it = next((i for i in items if i.get("name") == name), {})
                            title, body = self._fault_text(it)
                            if self._deliver(title, body):
                                self._state[name] = {"stage": "notified", "at": _now()}
                                acted["notified"].append(name)
                        else:
                            self._state.pop(name, None)
            self._save()
        return acted

    def loop(self):
        time.sleep(90)                      # let the watcher settle after boot
        while True:
            try:
                self.sweep()
            except Exception as e:          # noqa: BLE001 — never die
                print("  [proactive] sweep error: %s" % e, flush=True)
            time.sleep(_INTERVAL)

    def start(self):
        t = threading.Thread(target=self.loop, daemon=True, name="proos-proactive")
        t.start()
        print("  proactive Pro running (interval %ds, quiet %02d:00-%02d:00)"
              % (_INTERVAL, _QUIET_START, _QUIET_END), flush=True)
        return t
