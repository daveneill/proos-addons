"""
Channel lists — the house's channels per service, confirmed on a TV before anything uses them (register 1049).

Dave, 28 Oct 2026: "Never assume if not confirmed … design the channel logos and number editor … support free-to-air,
Foxtel and Fetch TV channel logos and numbers … built in Pro" (mockup approved 14:57, "yes build").

One list per SERVICE in the house (free-to-air for a region, Foxtel, Fetch TV, other). Every TV or box that uses a
service shares its list, so a number fixed once is fixed in every room. Each channel carries its number, name, other
names (for voice), a type ("put on the news"), shown-on-dashboards, a logo reference, and WHO confirmed it, WHEN and on
WHICH device. A channel that is not confirmed is never offered to a dashboard or to voice (confirmed()).

Rule Zero: ProOS's starting lists are a suggestion. Every row of one starts unconfirmed; changing a channel's number
clears its confirmation (the old check proved a different number). Nothing here measures what a TV is showing — sending
a number is SENT, never "on" (the TV page says "Sent 9").

Pure store over /data/channel_lists.json (a new name: an older "channel presets" store used channels.json, and a
box may still hold that file — it is never read or overwritten here). No platform calls here; the server sends the number through the platform's own
media_player.play_media (media_content_type "channel").
"""
import json
import os
import re
import time
import uuid

SERVICES = ("fta", "foxtel", "fetch", "other")
SERVICE_NAMES = {"fta": "Free-to-air", "foxtel": "Foxtel", "fetch": "Fetch TV", "other": "Other"}
TYPES = ("General", "News", "Sport", "Kids", "Movies", "Music")
_STARTS_FILE = os.path.join(os.path.dirname(__file__), "channel_starts.json")


def _path() -> str:
    return os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "channel_lists.json")   # NOT channels.json: an older store's name


def _all() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("lists"), dict):
            return d
    except Exception:                                            # noqa: BLE001
        pass
    return {"lists": {}}


def _write(d: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, path)


def _num(n) -> str:
    """A channel number as the TV takes it: digits only (1–4 of them). '' when it is not one."""
    s = str(n if n is not None else "").strip()
    return s if re.fullmatch(r"\d{1,4}", s) else ""


def _aka(v) -> list:
    if isinstance(v, str):
        v = v.split(",")
    return [x.strip() for x in (v or []) if isinstance(x, str) and x.strip()][:8]


def _summary(lst: dict) -> dict:
    ch = lst.get("channels") or []
    ok = sum(1 for c in ch if c.get("confirmed"))
    return {"id": lst["id"], "service": lst["service"], "service_name": SERVICE_NAMES.get(lst["service"], ""),
            "region": lst.get("region") or "", "name": lst.get("name") or "", "used_by": list(lst.get("used_by") or []),
            "count": len(ch), "confirmed": ok, "to_check": len(ch) - ok,
            "hidden": sum(1 for c in ch if not c.get("shown", True))}


# ── reading ────────────────────────────────────────────────────────────────────────────────────────────────────
def lists() -> list:
    """Every list in the house, summarised (counts, to-check, which devices use it)."""
    return [_summary(x) for x in _all()["lists"].values()]


def get(list_id: str):
    lst = _all()["lists"].get(list_id)
    if not lst:
        return None
    out = dict(lst)
    out["channels"] = sorted(lst.get("channels") or [], key=lambda c: int(c.get("number") or 0))
    out.update({k: v for k, v in _summary(lst).items() if k in ("count", "confirmed", "to_check", "hidden", "service_name")})
    return out


def confirmed(list_id: str, shown_only: bool = True) -> list:
    """ONLY confirmed channels — the ones a dashboard or voice may use. Hidden ones too when shown_only is False
    (voice and typed numbers may still use a hidden channel; a dashboard tile may not)."""
    lst = get(list_id)
    if not lst:
        return []
    return [c for c in lst["channels"] if c.get("confirmed") and (c.get("shown", True) or not shown_only)]


def list_for_device(entity_id: str):
    """The list a TV or box uses (it is named in that list's used_by), or None."""
    for x in _all()["lists"].values():
        if entity_id in (x.get("used_by") or []):
            return x["id"]
    return None


def starts() -> dict:
    """ProOS's starting lists that ship with Core: {service: {region: [rows]}} — suggestions, every row to check."""
    try:
        with open(_STARTS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def start_options() -> list:
    """What Pro offers under Start From: one entry per service/region ProOS has a starting list for."""
    out = []
    for svc, regions in starts().items():
        if svc.startswith("_") or not isinstance(regions, dict):
            continue
        for region, rows in regions.items():
            out.append({"service": svc, "region": region, "count": len(rows or [])})
    return out


# ── writing ────────────────────────────────────────────────────────────────────────────────────────────────────
def _channel(row: dict) -> tuple:
    """Build one channel from a request or a starting row. Returns (channel, error)."""
    no = _num(row.get("number"))
    name = (row.get("name") or "").strip()
    if not no:
        return None, "A channel number is 1 to 4 digits"
    if not name:
        return None, "A channel needs a name"
    typ = row.get("type") if row.get("type") in TYPES else "General"
    return {"id": row.get("id") or uuid.uuid4().hex[:10], "number": no, "name": name[:60], "aka": _aka(row.get("aka")),
            "type": typ, "shown": bool(row.get("shown", True)), "logo": (row.get("logo") or "").strip()[:120],
            "confirmed": None}, None


def create(service: str, region: str = "", name: str = "", used_by=None, start: str = "empty", by: str = "") -> dict:
    """A new list. start='proos' fills it from ProOS's starting list for that service and region — every row to
    check; start='empty' leaves it empty."""
    if service not in SERVICES:
        return {"error": "Choose Free-to-air, Foxtel, Fetch TV or Other"}
    d = _all()
    used = [e for e in (used_by or []) if isinstance(e, str) and e]
    taken = {e for x in d["lists"].values() for e in (x.get("used_by") or [])}
    clash = [e for e in used if e in taken]
    if clash:
        return {"error": "Already uses another list: " + ", ".join(clash)}
    rows = []
    if start == "proos":
        src = (starts().get(service) or {}).get(region)
        if not src:
            return {"error": "ProOS has no starting list for that yet — start empty"}
        for r in src:
            c, err = _channel(r)
            if c:
                rows.append(c)
    lid = uuid.uuid4().hex[:10]
    title = name.strip() or (SERVICE_NAMES[service] + (" · " + region if region else ""))
    d["lists"][lid] = {"id": lid, "service": service, "region": (region or "").strip()[:40], "name": title[:60],
                       "used_by": used, "channels": rows, "created": int(time.time()), "created_by": by or ""}
    _write(d)
    return {"ok": True, "list": _summary(d["lists"][lid])}


def set_used_by(list_id: str, used_by) -> dict:
    d = _all()
    lst = d["lists"].get(list_id)
    if not lst:
        return {"error": "No such list"}
    used = [e for e in (used_by or []) if isinstance(e, str) and e]
    other = {e for k, x in d["lists"].items() if k != list_id for e in (x.get("used_by") or [])}
    clash = [e for e in used if e in other]
    if clash:
        return {"error": "Already uses another list: " + ", ".join(clash)}
    lst["used_by"] = used
    _write(d)
    return {"ok": True, "list": _summary(lst)}


def rename(list_id: str, name: str) -> dict:
    d = _all()
    lst = d["lists"].get(list_id)
    if not lst or not (name or "").strip():
        return {"error": "No such list" if not lst else "A list needs a name"}
    lst["name"] = name.strip()[:60]
    _write(d)
    return {"ok": True}


def add_channel(list_id: str, row: dict) -> dict:
    d = _all()
    lst = d["lists"].get(list_id)
    if not lst:
        return {"error": "No such list"}
    c, err = _channel(row or {})
    if err:
        return {"error": err}
    if any(x.get("number") == c["number"] for x in lst["channels"]):
        return {"error": "Channel %s is already in this list" % c["number"]}
    lst["channels"].append(c)
    _write(d)
    return {"ok": True, "channel": c}


def update_channel(list_id: str, channel_id: str, fields: dict) -> dict:
    """Change a channel. A new NUMBER clears its confirmation — the old check was of a different number. Name, other
    names, type, shown and logo keep it (the number the TV was given is unchanged)."""
    d = _all()
    lst = d["lists"].get(list_id)
    c = next((x for x in (lst or {}).get("channels", []) if x.get("id") == channel_id), None)
    if not c:
        return {"error": "No such channel"}
    f = fields or {}
    if "number" in f:
        no = _num(f["number"])
        if not no:
            return {"error": "A channel number is 1 to 4 digits"}
        if no != c["number"]:
            if any(x.get("number") == no and x is not c for x in lst["channels"]):
                return {"error": "Channel %s is already in this list" % no}
            c["number"] = no
            c["confirmed"] = None
    if "name" in f:
        if not (f["name"] or "").strip():
            return {"error": "A channel needs a name"}
        c["name"] = f["name"].strip()[:60]
    if "aka" in f:
        c["aka"] = _aka(f["aka"])
    if "type" in f and f["type"] in TYPES:
        c["type"] = f["type"]
    if "shown" in f:
        c["shown"] = bool(f["shown"])
    if "logo" in f:
        c["logo"] = (f["logo"] or "").strip()[:120]
    _write(d)
    return {"ok": True, "channel": c}


def remove_channel(list_id: str, channel_id: str) -> dict:
    d = _all()
    lst = d["lists"].get(list_id)
    if not lst:
        return {"error": "No such list"}
    before = len(lst["channels"])
    lst["channels"] = [x for x in lst["channels"] if x.get("id") != channel_id]
    _write(d)
    return {"ok": True, "removed": before - len(lst["channels"])}


def confirm(list_id: str, channel_id: str, by: str, on_device: str, sent: bool) -> dict:
    """The installer looked at the TV after ProOS SENT this number to it, and says it is right. Refused unless this
    channel was sent to that device first (sent=True comes from the server's own record of the send)."""
    if not sent:
        return {"error": "Send it to the TV first, look, then confirm"}
    d = _all()
    lst = d["lists"].get(list_id)
    c = next((x for x in (lst or {}).get("channels", []) if x.get("id") == channel_id), None)
    if not c:
        return {"error": "No such channel"}
    c["confirmed"] = {"by": by or "", "at": int(time.time()), "on": on_device or "", "number": c["number"]}
    _write(d)
    return {"ok": True, "channel": c}


def unconfirm(list_id: str, channel_id: str) -> dict:
    d = _all()
    lst = d["lists"].get(list_id)
    c = next((x for x in (lst or {}).get("channels", []) if x.get("id") == channel_id), None)
    if not c:
        return {"error": "No such channel"}
    c["confirmed"] = None
    _write(d)
    return {"ok": True}


def remove_list(list_id: str) -> dict:
    """A list taken away is KEPT in removed (never thrown away), so an installer's mistake can be undone."""
    d = _all()
    lst = d["lists"].pop(list_id, None)
    if not lst:
        return {"error": "No such list"}
    lst["removed_at"] = int(time.time())
    d.setdefault("removed", []).append(lst)
    d["removed"] = d["removed"][-10:]
    _write(d)
    return {"ok": True}


# ── each room's favourite channels (register 1051) ──────────────────────────────────────────────────────────────
# Kept HERE, beside the lists — not in the room's App Shortcuts. Every reader of that list (voice "open Netflix", the
# app tiles, the launcher, the watch helper, the sweep) treats an entry as an app, so a channel there would be launched
# as one. One list per room all the same, synced to Pro and every dashboard the same way.
def _fav_ok(d: dict, list_id: str, channel_id: str, device: str):
    lst = d["lists"].get(list_id)
    if not lst:
        return None, "No such list"
    if device not in (lst.get("used_by") or []):
        return None, "That TV doesn\u2019t use this list"
    c = next((x for x in lst.get("channels", []) if x.get("id") == channel_id), None)
    if not c:
        return None, "No such channel"
    return c, None


def favourites(area_id: str, for_dashboard: bool = True) -> list:
    """The room's favourite channels in its order, each resolved from its list: {list, channel, device, number, name,
    logo, type}. For a dashboard only CONFIRMED and SHOWN channels (an unconfirmed or hidden one is left out, never
    shown as if it were fine); Pro sees every saved favourite with its state."""
    d = _all()
    out = []
    for f in (d.get("favs") or {}).get(area_id, []):
        lst = d["lists"].get(f.get("list"))
        c = next((x for x in (lst or {}).get("channels", []) if x.get("id") == f.get("channel")), None)
        if not c or f.get("device") not in ((lst or {}).get("used_by") or []):
            if not for_dashboard:
                out.append(dict(f, missing=True))
            continue
        ok = bool(c.get("confirmed")) and c.get("shown", True)
        if for_dashboard and not ok:
            continue
        out.append({"list": f["list"], "channel": c["id"], "device": f["device"], "number": c["number"], "name": c["name"],
                    "logo": c.get("logo") or "", "type": c.get("type") or "General",
                    "confirmed": bool(c.get("confirmed")), "shown": c.get("shown", True)})
    return out


def add_fav(area_id: str, list_id: str, channel_id: str, device: str) -> dict:
    """Only a CONFIRMED channel can become a favourite, and only for a TV that uses its list."""
    d = _all()
    c, err = _fav_ok(d, list_id, channel_id, device)
    if err:
        return {"error": err}
    if not c.get("confirmed"):
        return {"error": "Confirm %s on the TV first" % c["name"]}
    favs = d.setdefault("favs", {}).setdefault(area_id, [])
    if any(f.get("list") == list_id and f.get("channel") == channel_id and f.get("device") == device for f in favs):
        return {"ok": True, "count": len(favs)}
    favs.append({"list": list_id, "channel": channel_id, "device": device})
    _write(d)
    return {"ok": True, "count": len(favs)}


def remove_fav(area_id: str, list_id: str, channel_id: str, device: str) -> dict:
    d = _all()
    favs = (d.get("favs") or {}).get(area_id, [])
    keep = [f for f in favs if not (f.get("list") == list_id and f.get("channel") == channel_id and f.get("device") == device)]
    d.setdefault("favs", {})[area_id] = keep
    _write(d)
    return {"ok": True, "removed": len(favs) - len(keep)}


def move_fav(area_id: str, list_id: str, channel_id: str, device: str, step: int) -> dict:
    """One place earlier (step -1) or later (+1) in the room's order."""
    d = _all()
    favs = (d.get("favs") or {}).get(area_id, [])
    i = next((n for n, f in enumerate(favs) if f.get("list") == list_id and f.get("channel") == channel_id and f.get("device") == device), -1)
    j = i + (1 if step > 0 else -1)
    if i < 0 or j < 0 or j >= len(favs):
        return {"ok": True, "moved": 0}
    favs[i], favs[j] = favs[j], favs[i]
    _write(d)
    return {"ok": True, "moved": 1}


def send_command(entity_id: str, number: str, platform: str) -> tuple:
    """Pure: the platform service call that sends a channel number — the integration's OWN channel command. Only the
    Samsung driver is used for a tuner today (play_media channel 'N@TV': it selects the tuner, then the number).
    Returns ((domain, service, data), error)."""
    no = _num(number)
    if not no:
        return None, "A channel number is 1 to 4 digits"
    if platform == "samsungtv_smart":
        return ("media_player", "play_media", {"entity_id": entity_id, "media_content_type": "channel",
                                               "media_content_id": no + "@TV"}), None
    return None, "ProOS can't send a channel number to this device yet"


def clear() -> None:
    """Factory reset only."""
    try:
        if os.path.exists(_path()):
            os.remove(_path())
    except Exception:                                            # noqa: BLE001
        pass
