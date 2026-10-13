"""SAY AT ONCE, VERIFY AFTER — the Assist plan, build 4 (register 856; 14 Oct 2026).

An instant sentence is answered by the platform the moment it matches ("Bedroom, Netflix.") and the act follows — the only way
to be as quick as Josh, and what Josh does. The reply is therefore a claim made BEFORE the device has done it, and the
product's law is actual state, never a claim (738: "said I couldn't — but started playing anyway"). So every instant act is
CHECKED afterwards, from the device's own state through the platform, and the dashboards are told only when the claim turned
out wrong: "Bedroom: the Bedroom TV didn't open Netflix (it shows HDMI 2)". A claim that proved true is logged with the
time it took and nothing is said twice.
"""
from __future__ import annotations

import time

APP_WAIT_S = 8.0        # plumbing: how long a TV or box is given to show the app after the launch was accepted
PLAY_WAIT_S = 10.0      # plumbing: how long a speaker is given to say "playing" after the platform took the favourite
STEP_S = 0.5


def _state(client, eid):
    try:
        return client._req("GET", "/api/states/%s" % eid) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def _norm(s):
    return " ".join(str(s or "").lower().replace("+", " plus").split())


def check_app(client, device, app, wait_s=None, now=time.time, sleep=time.sleep):
    """(confirmed, seconds, words). Confirmed when the device's own source or app_name is the app."""
    wait_s = APP_WAIT_S if wait_s is None else wait_s
    t0 = now()
    last = {}
    while now() - t0 <= wait_s:
        st = _state(client, device)
        a = st.get("attributes") or {}
        shown = a.get("app_name") or a.get("source") or ""
        if _norm(shown) == _norm(app) or (_norm(app) and _norm(app) in _norm(shown)):
            return True, round(now() - t0, 1), "%s shows %s" % (a.get("friendly_name") or device, shown)
        last = {"state": st.get("state"), "shown": shown, "name": a.get("friendly_name") or device}
        sleep(STEP_S)
    why = ("it is %s" % last.get("state")) if last.get("state") in (None, "off", "unavailable", "standby") else \
          ("it shows %s" % last.get("shown") if last.get("shown") else "it shows nothing")
    return False, round(now() - t0, 1), "the %s didn't open %s (%s)" % (last.get("name") or device, app, why)


def check_play(client, entity, name, wait_s=None, now=time.time, sleep=time.sleep):
    """(confirmed, seconds, words). Confirmed when the speaker itself says playing."""
    wait_s = PLAY_WAIT_S if wait_s is None else wait_s
    t0 = now()
    last = {}
    while now() - t0 <= wait_s:
        st = _state(client, entity)
        a = st.get("attributes") or {}
        if st.get("state") == "playing":
            return True, round(now() - t0, 1), "%s is playing %s" % (a.get("friendly_name") or entity, a.get("media_title") or name)
        last = {"state": st.get("state"), "name": a.get("friendly_name") or entity}
        sleep(STEP_S)
    return False, round(now() - t0, 1), "the %s didn't start %s (it is %s)" % (last.get("name") or entity, name, last.get("state") or "unknown")


def correction(room, words):
    """What the dashboards show when the instant reply proved wrong — the room first, the device's own state inside."""
    return "%s: %s." % (str(room or "").strip(), str(words or "").strip().rstrip("."))
