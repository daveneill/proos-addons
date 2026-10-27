"""THE PLATFORM'S OWN INTEGRATION LOG, IN ProOS's MASTER LOG (register 1027; Dave, 27 Oct 2026).

Dave, 10:41: "At the moment we need to be reporting in our logs what is in the platforms logs from the integrations
when i am happy that we are reporting exactly as the platform reports then we can make any rules on how ProOS handles
situations and what flags faults … in assists and installers view PROos is the platform". 11:16: "developer access gets
everything word for word .. tech and installer modes logs need to then be filtered to never show Home Assistant name it
is to be replaced with ProOS".

Read on his box the same morning (register 1026): the platform's system log held 1,582 integration messages in 24 h and
ProOS's Master Log showed none of them.

WHAT THIS IS: a mirror of the platform's system log (WebSocket system_log/list), nothing more. One row per message as
the platform groups it — integration, level, the platform's own message lines, how many times, first and last seen.
No rule, no verdict, no fault is drawn from any row (Dave: not until he says the logs match).
  * The platform keeps its log in memory and starts counting again at its own restart; ProOS keeps each message's
    total across those restarts (the platform's count for the current run + the counts of runs already seen).
  * A row is attached to a ROOM only where the message itself names a device by identity: an entity id the platform
    holds, or a network address that is a configured integration's own host, and only when that names one room.
    Otherwise it belongs to the whole system. Names are never matched.
  * Developer: every word as the platform wrote it (plus its logger and source). Tech / installer: the same words with
    the platform's name replaced by "ProOS" (Dave, 11:16) — nothing else changed.
Pure functions; the server injects the readings.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

DATA = os.environ.get("PROOS_DATA_DIR", "/data")
STORE = os.path.join(DATA, "platform_log.json")
# PLUMBING: how long a message that has stopped occurring stays in ProOS's log, and how many distinct messages are kept.
KEEP_S = 7 * 86400
KEEP_N = 500

_lock = threading.Lock()
_rows = None

# The platform's name in every spelling its logs use — "Home Assistant", "homeassistant", "home-assistant",
# "home_assistant", "HASS", "hass", "hassio" (the white-label rule names "Home Assistant" / "HASS"; whitelabel_bench).
_PLATFORM_NAME = re.compile(r"home[\s_\-]?assistant|(?<![a-z])hass(?:io)?(?![a-z])", re.I)
_ENTITY = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")
_IP = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def integration_of(row: dict) -> str:
    """The integration the platform's own source path names; else the logger's top package."""
    src = row.get("source") or []
    path = str(src[0] if isinstance(src, (list, tuple)) and src else src or "")
    m = re.search(r"(?:components|custom_components)/([a-z0-9_]+)/", path)
    if m:
        return m.group(1)
    name = str(row.get("name") or "")
    m = re.match(r"(?:homeassistant\.components|custom_components)\.([a-z0-9_]+)", name)
    if m:
        return m.group(1)
    return name.split(".")[0] or "system"


def _key(row: dict) -> str:
    src = row.get("source") or []
    msg = row.get("message") or [""]
    first = str(msg[0] if isinstance(msg, (list, tuple)) and msg else msg)
    return "%s|%s|%s" % (row.get("name"), "%s:%s" % tuple((list(src) + ["", ""])[:2]) if isinstance(src, (list, tuple)) else src,
                         first[:300])


def _load():
    global _rows
    if _rows is None:
        try:
            with open(STORE, encoding="utf-8") as fh:
                _rows = json.load(fh) or {}
        except Exception:                                        # noqa: BLE001
            _rows = {}
    return _rows


def _save():
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        with open(STORE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(_rows or {}, fh)
        os.replace(STORE + ".tmp", STORE)
    except Exception:                                        # noqa: BLE001
        pass


def attach(message: str, ent_area: dict, host_areas: dict):
    """The one room a message names by identity, or None. ent_area: {entity_id: area_id}; host_areas: {ip: {area_id,…}}."""
    areas = set()
    for e in _ENTITY.findall(message or ""):
        if e in ent_area and ent_area[e]:
            areas.add(ent_area[e])
    for ip in _IP.findall(message or ""):
        areas |= set(a for a in (host_areas.get(ip) or ()) if a)
    return next(iter(areas)) if len(areas) == 1 else None


def ingest(platform_rows, now=None, ent_area=None, host_areas=None) -> dict:
    """Merge one system_log/list read. Returns the store."""
    now = now if now is not None else time.time()
    with _lock:
        rows = _load()
        for r in platform_rows or []:
            k = _key(r)
            msg = r.get("message") or []
            text = "\n".join(str(x) for x in (msg if isinstance(msg, (list, tuple)) else [msg]))
            cur_first = r.get("first_occurred") or r.get("timestamp")
            cnt = int(r.get("count") or 1)
            rec = rows.get(k)
            if rec is None:
                rec = {"integration": integration_of(r), "logger": r.get("name"), "level": r.get("level"),
                       "source": r.get("source"), "message": text, "base": 0, "cur_first": cur_first,
                       "cur_count": 0, "first": cur_first}
                rows[k] = rec
            elif rec.get("cur_first") != cur_first:
                rec["base"] = int(rec.get("base") or 0) + int(rec.get("cur_count") or 0)     # a new platform run
                rec["cur_first"] = cur_first
            rec["cur_count"] = cnt
            rec["count"] = int(rec.get("base") or 0) + cnt
            rec["last"] = r.get("timestamp") or now
            rec["message"] = text
            rec["level"] = r.get("level")
            rec["room"] = attach(text, ent_area or {}, host_areas or {})
        # keep the store bounded: drop messages that stopped occurring KEEP_S ago, then the oldest beyond KEEP_N
        for k in [k for k, v in rows.items() if now - float(v.get("last") or 0) > KEEP_S]:
            rows.pop(k, None)
        if len(rows) > KEEP_N:
            for k in sorted(rows, key=lambda x: float(rows[x].get("last") or 0))[:len(rows) - KEEP_N]:
                rows.pop(k, None)
        _save()
        return dict(rows)


def for_viewer(developer: bool, since=None) -> list:
    """Master Log rows (newest occurrence first). Developer: word for word. Tech / installer: the platform's name
    replaced by "ProOS", nothing else changed; no logger or source path."""
    with _lock:
        rows = dict(_load())
    out = []
    for k, v in rows.items():
        last = float(v.get("last") or 0)
        if since and last < since:
            continue
        msg = v.get("message") or ""
        data = {"integration": v.get("integration"), "level": v.get("level"), "message": msg,
                "count": v.get("count"), "first": v.get("first"), "last": last}
        if developer:
            data.update({"logger": v.get("logger"), "source": v.get("source")})
        else:
            data["message"] = _PLATFORM_NAME.sub("ProOS", msg)
            data["integration"] = _PLATFORM_NAME.sub("ProOS", str(data["integration"] or ""))
        out.append({"ts": last, "room": v.get("room") or "site", "type": "integration_log", "data": data})
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out
