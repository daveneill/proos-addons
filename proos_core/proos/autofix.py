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
