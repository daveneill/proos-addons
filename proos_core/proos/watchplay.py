"""ProOS Core — "find me something to watch": what a room can play, starting a show, and learning what works. 970oo.

Dave, 23 Oct 2026, 14:57: "if I asked about a suggestion to watch it could make a suggestion on any service and know if
it's able to play it based on the available devices in a room … I need assist to learn based on what you ask it".

THE RECORD, NOT A LIST (register 867): what a room can play is its APP SHORTCUTS — the installer's (or Assist's, tested)
record of app + device. Devices that only SAY they carry an app are offered for setting up, never launched.

HOW FAR EACH ONE GOES — three levels, per device and app:
  direct  — straight to the show, playing. Only from a method SEEN working on this box and confirmed by the person.
  app     — the app opens; the person picks the show.
  (a device and app with a candidate method nobody has confirmed is "untested": tried, then the person is ASKED, and
   the answer is recorded — that is how it learns. Nothing is written that the person did not confirm.)

THE ONE METHOD SEEN WORKING (register 970nn, Bedroom Shield, 14:35–14:40): an Android TV box (the platform's Android
TV Remote integration) — the room's watch activity for the box, then the title's link sent as the remote's
`turn_on` activity (Netflix: `https://www.netflix.com/title/<id>?source=30`, from Dave's guide), sent twice from
standby ~30 s apart; Netflix opens on the title page at the next episode; the remote's Select starts it. Apple TV:
Netflix opened to its home screen for every link form tried — app only.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import shortcuts as _sc

ANDROID = ("androidtv_remote",)          # the platform whose remote takes a link as its turn_on activity (970nn)
FIRST_SEND_WAIT_S = 3.0                  # plumbing: after the app opens, before the first link
SECOND_SEND_WAIT_S = 30.0                # plumbing: Netflix forgets a link sent while it is starting (guide: 25 s; 30 s seen)
SELECT_WAIT_S = 8.0                      # plumbing: the title page settling before Select (seen 14:40)

# How a title's web link becomes the link a box opens it with. Only rows SEEN working are here; a service with no row
# has its own web link sent unchanged, as an untested try.
TITLE_LINKS = {
    "netflix": {"pattern": r"netflix\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?title/(\d+)",
                "link": "https://www.netflix.com/title/{id}?source=30"},       # guide ch. 8; seen 970nn
}


def _path() -> str:
    return os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "watch_learned.json")


def _load() -> dict:
    try:
        with open(_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _key(target: str, app: str) -> str:
    return "%s|%s" % (target, _sc.canonical_slug(app))


def learned(target: str, app: str) -> dict:
    return _load().get(_key(target, app)) or {}


def learn(target: str, app: str, played: bool, who: str = "", note: str = "") -> dict:
    """What the PERSON confirmed after a try: played → direct; not → app (the link method is not used again)."""
    if not target or not app:
        return {"error": "device and app required"}
    d = _load()
    row = {"level": "direct" if played else "app", "when": time.strftime("%Y-%m-%d %H:%M"), "by": who or "",
           "note": (note or "")[:200]}
    d[_key(target, app)] = row
    tmp = _path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, _path())
    return dict(row, ok=True, device=target, app=_sc.canonical_slug(app))


def title_link(app: str, web_link: str) -> str:
    """The link a box is sent for a title, from the title's web address (found by web search). '' when none given."""
    w = str(web_link or "").strip()
    if not w.startswith(("http://", "https://")):
        return ""
    row = TITLE_LINKS.get(_sc.canonical_slug(app))
    if row:
        m = re.search(row["pattern"], w)
        return row["link"].format(id=m.group(1)) if m else ""
    return w


def level_of(sc: dict) -> str:
    """direct | app | untested — for one of the room's shortcuts."""
    got = learned(sc.get("target") or "", sc.get("app") or sc.get("name") or "").get("level")
    if got in ("direct", "app"):
        return got
    return "untested" if sc.get("platform") in ANDROID else "app"


def options(client, project, area_id: str, app: str = None) -> dict:
    """What this room can play: each app shortcut with its device and how far it goes; plus, for an app asked about,
    the devices that say they carry it but are not set up (to OFFER, never to launch)."""
    rows = []
    for sc in _sc.room(area_id) or []:
        if app and not (_sc._same(sc.get("name"), app) or _sc._same(sc.get("app"), app)
                        or _sc._same(_sc.canonical_slug(app), sc.get("app"))):
            continue
        st = _sc._state(client, sc.get("target") or "")
        rows.append({"app": sc.get("name") or sc.get("app"), "device": sc.get("target"),
                     "device_name": ((st.get("attributes") or {}).get("friendly_name") or sc.get("target")),
                     "level": level_of(sc)})
    out = {"area_id": area_id, "can_play": rows,
           "levels": {"direct": "straight to the show, playing", "app": "opens the app; they pick the show",
                      "untested": "will try straight to the show, then ask whether it played"}}
    if app and not rows:
        out["not_set_up"] = _sc.could_run(client, project, area_id, app)
    return out


def _remote_for(client, media_player: str) -> str:
    """The remote entity on the same device as an Android TV media player (the platform's registry), or ''."""
    try:
        reg = client.entity_registry() or []
        dev = next((e.get("device_id") for e in reg if e.get("entity_id") == media_player), None)
        if dev:
            return next((e.get("entity_id") for e in reg
                         if e.get("device_id") == dev and str(e.get("entity_id") or "").startswith("remote.")), "") or ""
    except Exception:                                    # noqa: BLE001
        pass
    return ""


def _send_title(client, remote: str, link: str, was_asleep: bool, sleep=time.sleep) -> None:
    """The sequence seen working (970nn): link; from standby, link again ~30 s later; then Select."""
    sleep(FIRST_SEND_WAIT_S)
    client._req("POST", "/api/services/remote/turn_on", {"entity_id": remote, "activity": link})
    if was_asleep:
        sleep(SECOND_SEND_WAIT_S)
        client._req("POST", "/api/services/remote/turn_on", {"entity_id": remote, "activity": link})
    sleep(SELECT_WAIT_S)
    client._req("POST", "/api/services/remote/send_command", {"entity_id": remote, "command": "DPAD_CENTER"})


def play(client, project, area_id: str, app: str, web_link: str = "", device: str = None, spawn=None,
         sleep=time.sleep) -> dict:
    """Start a title in the room: the app opens through the room's shortcut (power, input and app — the proven launch);
    on an Android TV box whose level is direct or untested, the title's link and Select follow in the background.
    Returns at once with how far it will go."""
    sc = _sc.find(area_id, app, device) or (None if device else _sc.find(area_id, app))
    if sc is None:
        return {"ok": False, "not_set_up": True, "devices": _sc.could_run(client, project, area_id, app),
                "error": "%s isn't set up in this room" % app}
    lvl = level_of(sc)
    link = title_link(app, web_link) if lvl in ("direct", "untested") else ""
    target = sc.get("target")
    before = str((_sc._state(client, target) or {}).get("state") or "")
    asleep = before in ("off", "standby", "unavailable", "unknown", "")
    res = _sc.launch(client, project, area_id, sc) or {}
    if not res.get("ok"):
        return dict(res, level=lvl)
    out = {"ok": True, "app": sc.get("name") or sc.get("app"), "device": target, "level": lvl if link else "app",
           "launch": {k: res.get(k) for k in ("confirmed", "taken", "opened", "note")}}
    if link:
        remote = _remote_for(client, target)
        if not remote:
            out["level"] = "app"
            out["note"] = "no remote for this box in the platform — the app is open; pick the show"
            return out
        (spawn or (lambda f: threading.Thread(target=f, daemon=True).start()))(
            lambda: _send_title(client, remote, link, asleep, sleep))
        out["starting"] = True
        out["link"] = link
        out["say"] = "Starting it now — about half a minute%s." % (
            "" if lvl == "direct" else "; tell me whether it played and I'll remember for next time")
        out["ask_after"] = lvl == "untested"
    else:
        out["say"] = "%s is open — pick it from there." % (sc.get("name") or sc.get("app"))
    return out
