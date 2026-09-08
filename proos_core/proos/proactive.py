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
_QUIET_START, _QUIET_END = 22, 7


def _now() -> float:
    return time.time()


def _quiet() -> bool:
    h = datetime.now().hour
    return h >= _QUIET_START or h < _QUIET_END


class Proactive:
    def __init__(self, client, watcher, enabled=lambda: True):
        self.client = client
        self.watcher = watcher
        self.enabled = enabled
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

    # -- the sweep -----------------------------------------------------------
    def sweep(self) -> dict:
        """One pass. Returns what it did (also used by the test endpoint)."""
        if not self.enabled():
            return {"enabled": False}
        rep = (self.watcher.report() if self.watcher else None) or {}
        items = rep.get("items") or []
        acted = {"notified": [], "recovered": [], "queued": [], "skipped": []}
        with self._lock:
            seen_faults = set()
            for it in items:
                name = it.get("name") or ""
                if not name:
                    continue
                rec = self._state.get(name) or {}
                if it.get("status") == "fault":
                    seen_faults.add(name)
                    already = rec.get("stage")
                    if already == "notified" and _now() - rec.get("at", 0) < _COOLDOWN:
                        acted["skipped"].append(name)
                        continue
                    if _quiet():
                        # Queue it: announced in the morning if still faulted.
                        self._state[name] = {"stage": "queued", "at": _now()}
                        acted["queued"].append(name)
                        continue
                    title, body = self._fault_text(it)
                    if self._deliver(title, body):
                        self._state[name] = {"stage": "notified", "at": _now()}
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
                    elif rec.get("stage") == "queued":
                        # Fixed before anyone was told — say nothing, ever.
                        self._state.pop(name, None)
            # Morning flush: queued faults that are STILL faulted get announced
            # once quiet hours end (they're in seen_faults, stage queued).
            if not _quiet():
                for name, rec in list(self._state.items()):
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
