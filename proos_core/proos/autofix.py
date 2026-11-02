"""HEALTH HANDS A DEVICE TO THE AI ONCE A DAY, NOT EVERY 15 MINUTES — register 1164 (2 Nov 2026).

Dave 07:07: "get rid of this fault going to AI every 15 mins"; ruled the same minute: "Once per device per day". Read:
the watcher's auto-heal hands a faulted device to Assist up to 3 times, 15 min apart — but that count starts again
whenever the device flickers back for a moment and whenever Core restarts, so a lamp that drops in and out (Bec's Lamp)
went to the model again and again (~US$0.09 a turn on gpt-6.1-sol, 5–6 rounds each — the 1162 ledger). Now the AUTOMATIC
hand-over is allowed once per device per 24 h, remembered on disk across restarts; a device still faulted stays on Health
for the installer as before. The Fix button (Pro, Health cards) does not come through here — it hands over whenever
pressed. This is the recovery layer (policy, Dave's ruling), not the observation layer.
"""
from __future__ import annotations

import json
import os
import threading
import time

PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "assist_autofix.json")
EVERY_S = 24 * 3600          # Dave's ruling, 2 Nov 2026: once per device per day
_KEEP_S = 7 * 86400          # plumbing: how long a device's last hand-over is kept on file
_LOCK = threading.Lock()


def allowed(entity, now=None) -> bool:
    """True (and the hand-over recorded) when this device has not been handed to the AI automatically in 24 h."""
    now = now or time.time()
    key = str(entity or "")
    with _LOCK:
        try:
            with open(PATH, encoding="utf-8") as fh:
                seen = json.load(fh)
            seen = seen if isinstance(seen, dict) else {}
        except Exception:                                        # noqa: BLE001
            seen = {}
        last = seen.get(key)
        if isinstance(last, (int, float)) and now - last < EVERY_S:
            return False
        seen[key] = now
        seen = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and now - v < _KEEP_S}
        try:
            os.makedirs(os.path.dirname(PATH), exist_ok=True)
            tmp = PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(seen, fh)
            os.replace(tmp, PATH)
        except Exception:                                        # noqa: BLE001
            pass
        return True


def clear() -> None:
    try:
        os.remove(PATH)
    except FileNotFoundError:
        pass


# ── REGISTER 1180 — EVERY HEALTH PROBLEM GOES TO ASSIST, NOT ONLY A DROPPED DEVICE (the audit's item 2) ────────────────
# Dave 21:23: "this product was meant to be able to report and fix issues with integrations, connections anything but I
# spend so much time just reporting it to you"; the audit (1178) read that only ONE problem kind was ever acted on without
# a button (a watched device's fault); 22:29 "Just go". The 14 Sep ruling decides the shape: no scripted ladder — the
# problem goes to Assist, which diagnoses and fixes it as a Developer session with the platform does. The same once-a-day
# rule (2 Nov) bounds it: one automatic hand-over per subject per day, whichever road it came by.
#
# Which problems go: the ones that are something BROKEN in the running home. Not: a commissioning choice the installer
# makes (a room renamed, an input unassigned, a witness to bind, a display's settings, a record to rebuild, a commit to
# redo, a device missing from the platform), a confirmation that needs someone to LOOK (27 Oct ruling), or an update
# that is simply available. Those keep their buttons in Pro.
AUTO_KINDS = ("activity_missed", "committed_unavailable", "frozen_session", "link_unstable", "off_with_traffic",
              "witness_blackout", "witness_broken", "music_wedged", "music_service_slow", "provider_auth",
              "infra_down", "network_blind", "core_blind", "push_failing")


def health_subject(inc: dict) -> str:
    """The one name a Health problem is rate-limited by: its subject (a device) or, without one, its own id."""
    return str((inc or {}).get("subject") or (inc or {}).get("id") or (inc or {}).get("kind") or "")


def health_brief(inc: dict) -> str:
    """What Assist is handed for a Health problem — the problem as Health worded it, and the job."""
    inc = inc or {}
    return ("Health has raised a problem in this home and it needs fixing: \"%s\"%s. What Health read: %s "
            "Diagnose it as you would for a pro: read what has already been tried and what each attempt read back "
            "(recovery_history), the devices' live state (device_liveness, get_states), and the platform's own word on "
            "the integration and its log (ha_get_integration, ha_get_logs). Then act through the platform's own controls "
            "(device_recover; ha_call_service, including a button.press the integration offers), reading back after "
            "each act, until it is fixed or you have established why it cannot be. Do not start, turn on or play "
            "anything the household has not just asked for. Report in plain words what you found, what you did and what "
            "each act read back. Do not repeat a fix that read back as changing nothing."
            % (inc.get("title") or inc.get("kind") or "a problem",
               (" (%s)" % inc.get("room")) if inc.get("room") else "",
               str(inc.get("cause") or "no detail.").strip()))


# ── REGISTER 1182 — A PLATFORM WARNING THAT KEEPS REPEATING GOES TO ASSIST (the audit's item 4) ────────────────────────
# Read 2 Nov: "Updating samsungtv_smart media_player took longer than the scheduled update interval" — 379 times since
# 25 Oct, mirrored into the Master Log (1027) and never acted on; the Bedroom TV read "off" unchanged for a day through it.
# Dave 3 Nov 08:58, answering "how many times should the same warning repeat in an hour before ProOS treats it as a
# problem? I'd suggest 10": "yes". The count is the platform's OWN (system_log's running count per message); ProOS only
# compares two of its readings an hour apart.
REPEAT_PER_HOUR = 10          # Dave's ruling, 3 Nov 2026
_REPEAT_WINDOW_S = 3600
_seen_counts: dict = {}       # key -> [(ts, platform count)], the last hour


def _log_key(row: dict) -> str:
    msg = row.get("message")
    msg = msg[0] if isinstance(msg, list) and msg else msg
    return "%s|%s" % (row.get("name") or "", str(msg or "")[:160])


def repeating(rows: list, now=None) -> list:
    """The platform log rows (WARNING or ERROR) whose own count rose by REPEAT_PER_HOUR or more within the last hour.
    Each row is returned once per rise (its history restarts), with "rose" = how many times in that hour."""
    now = time.time() if now is None else now
    out = []
    live = set()
    for row in rows or []:
        if str(row.get("level") or "").upper() not in ("WARNING", "ERROR"):
            continue
        try:
            count = int(row.get("count") or 0)
        except Exception:                                        # noqa: BLE001
            continue
        key = _log_key(row)
        live.add(key)
        hist = [(t, c) for t, c in _seen_counts.get(key, []) if now - t <= _REPEAT_WINDOW_S and c <= count]
        hist.append((now, count))
        rose = count - hist[0][1]
        if rose >= REPEAT_PER_HOUR:
            out.append(dict(row, rose=rose, key=key))
            hist = [(now, count)]
        _seen_counts[key] = hist
    for key in [k for k in _seen_counts if k not in live]:
        _seen_counts.pop(key, None)                              # the platform dropped it (restart, cleared)
    return out


def repeat_incident(row: dict) -> dict:
    """A repeating platform warning, worded as a Health problem for Assist (health_brief reads it)."""
    msg = row.get("message")
    msg = msg[0] if isinstance(msg, list) and msg else msg
    who = str(row.get("name") or "the platform").replace("homeassistant.components.", "").replace("custom_components.", "")
    return {"id": "log:" + row.get("key", _log_key(row))[:120], "kind": "platform_warning_repeating",
            "subject": "log:" + row.get("key", _log_key(row))[:120],
            "title": "%s keeps warning" % who,
            "cause": "The platform logged \"%s\" %d times in the last hour (%s times since it first appeared)."
                     % (str(msg or "")[:240], int(row.get("rose") or 0), row.get("count"))}
