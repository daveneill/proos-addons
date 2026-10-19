"""ProOS Core - a real picture of a wall panel's screen (register 963).

Dave, 19 Oct 2026 10:16: Pro shows "a real picture of the screen that just updates when you navigate to the page that
shows it so not live just updates each time". 10:31, option 1: Core asks the panel's own screen software for the picture.

One job - take(screen) - with one small connector per kind of panel, named by the screen when it registers itself
(layouts.SHOTS):
  "kiosk"  the box's own kiosk screen app (the G-Flip): its REST door on the box's internal network, with the key Core
           made (panelsetup.kiosk_rest). The kiosk writes the picture into the shared media folder; Core reads it back.
A screen with no connector answers {"why": ...}; Pro then says no picture is available. A picture is never reused as
a new one: each picture has a name of its own, made for this request, and is removed once read.
"""
import json
import os
import time
import urllib.request

MEDIA_DIR = os.environ.get("PROOS_MEDIA_DIR", "/media")
SHOT_PREFIX = "proos-panel-"
QUALITY = 80
# 963b, read on the box (ls /media/screenshots, 11:21): the kiosk's screenshot tool (scrot) NEVER overwrites — asked for a name that exists it writes
# "<name>_000.jpg", "_001" … beside it. So every picture gets a name of its own, is read back, and is removed at once;
# any of ours left behind (a crash between write and read) is swept on the next picture. Pictures never pile up.


def _dir() -> str:
    return os.path.join(MEDIA_DIR, "screenshots")


def _sweep(keep: str = "", older_than: float = 30):
    """Remove pictures of ours ("proos-panel…", including scrot's "_000" copies) older than a few seconds — never one
    another request is still reading — and never anything else in the folder."""
    now = time.time()
    try:
        for f in os.listdir(_dir()):
            if not f.startswith("proos-panel") or f == keep:
                continue
            p = os.path.join(_dir(), f)
            try:
                if now - os.path.getmtime(p) > older_than:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _post(url: str, token: str, body: dict, timeout: float = 15):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - the box's own internal address
        return json.loads(r.read().decode() or "{}")


def _kiosk(door: dict, post=_post) -> dict:
    if not door:
        return {"why": "Core cannot read the box's internal address, so it cannot reach the screen app"}
    _sweep()
    name = "%s%d.jpg" % (SHOT_PREFIX, int(time.time() * 1000))
    path = os.path.join(_dir(), name)
    try:
        r = post("http://%s:%d/screenshot" % (door["ip"], int(door["port"])), door["token"],
                 {"filename": name, "quality": QUALITY})
    except Exception as e:  # noqa: BLE001
        return {"why": "The screen app did not answer (%s)" % str(e)[:160]}
    if not (isinstance(r, dict) and r.get("success")):
        return {"why": "The screen app could not take the picture (%s)" % str(r)[:160]}
    for _ in range(20):                      # the app answers once the file is written; allow the disk a moment
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            if data:
                at = os.path.getmtime(path)
                try:
                    os.remove(path)          # read; ours; gone
                except OSError:
                    pass
                return {"jpeg": data, "at": at}
        except OSError:
            pass
        time.sleep(0.25)
    return {"why": "The screen app said it took the picture, but no new picture arrived"}


def take(record: dict, door_fn=None, post=_post) -> dict:
    """A new picture of one screen. record: the screen as layouts holds it. Returns {jpeg, at} or {why}."""
    shot = (record or {}).get("shot") or ""
    if shot == "kiosk":
        if door_fn is None:
            from proos import panelsetup
            door_fn = panelsetup.kiosk_rest
        return _kiosk(door_fn(), post)
    return {"why": "This screen cannot send a picture of itself"}
