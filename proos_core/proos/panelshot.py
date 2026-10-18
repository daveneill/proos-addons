"""ProOS Core - a real picture of a wall panel's screen (register 963).

Dave, 19 Oct 2026 10:16: Pro shows "a real picture of the screen that just updates when you navigate to the page that
shows it so not live just updates each time". 10:31, option 1: Core asks the panel's own screen software for the picture.

One job - take(screen) - with one small connector per kind of panel, named by the screen when it registers itself
(layouts.SHOTS):
  "kiosk"  the box's own kiosk screen app (the G-Flip): its REST door on the box's internal network, with the key Core
           made (panelsetup.kiosk_rest). The kiosk writes the picture into the shared media folder; Core reads it back.
A screen with no connector answers {"why": ...}; Pro then says no picture is available. A picture is never reused as
a new one: the file must be newer than the request, or the answer is a failure saying so.
"""
import json
import os
import time
import urllib.request

MEDIA_DIR = os.environ.get("PROOS_MEDIA_DIR", "/media")
SHOT_NAME = "proos-panel.jpg"          # one file, replaced each time - pictures never pile up in the media folder
QUALITY = 80


def _shot_path() -> str:
    return os.path.join(MEDIA_DIR, "screenshots", SHOT_NAME)


def _post(url: str, token: str, body: dict, timeout: float = 15):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - the box's own internal address
        return json.loads(r.read().decode() or "{}")


def _kiosk(door: dict, post=_post) -> dict:
    if not door:
        return {"why": "Core cannot read the box's internal address, so it cannot reach the screen app"}
    path = _shot_path()
    try:
        before = os.path.getmtime(path)
    except OSError:
        before = 0.0
    asked = time.time()
    try:
        r = post("http://%s:%d/screenshot" % (door["ip"], int(door["port"])), door["token"],
                 {"filename": SHOT_NAME, "quality": QUALITY})
    except Exception as e:  # noqa: BLE001
        return {"why": "the screen app did not answer (%s)" % str(e)[:160]}
    if not (isinstance(r, dict) and r.get("success")):
        return {"why": "the screen app could not take the picture (%s)" % str(r)[:160]}
    for _ in range(20):                      # the app answers once the file is written; allow the disk a moment
        try:
            m = os.path.getmtime(path)
            if m > before and m >= asked - 2:
                with open(path, "rb") as fh:
                    return {"jpeg": fh.read(), "at": m}
        except OSError:
            pass
        time.sleep(0.25)
    return {"why": "the screen app said it took the picture, but no new picture arrived"}


def take(record: dict, door_fn=None, post=_post) -> dict:
    """A new picture of one screen. record: the screen as layouts holds it. Returns {jpeg, at} or {why}."""
    shot = (record or {}).get("shot") or ""
    if shot == "kiosk":
        if door_fn is None:
            from proos import panelsetup
            door_fn = panelsetup.kiosk_rest
        return _kiosk(door_fn(), post)
    return {"why": "this screen cannot send a picture of itself"}
