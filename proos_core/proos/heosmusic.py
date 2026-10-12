"""THE SPEAKER'S OWN MUSIC ON HEOS (Denon, Marantz and any HEOS player) — music design stage 5.

REGISTERS 766–768 (12 Oct 2026, read on Dave's Marantz SR5014 before a line was written):
  - The PLATFORM's HEOS integration already lists the player's music services (with the signed-in
    account), browses into them, and plays any item it lists — and it also plays an address Core
    builds itself in the platform's own form (heos://media/<sid>/<type>?…; Triple M Radio, playing
    9 s after asking, title and artwork filled in by the platform). So ProOS uses the platform for
    the services, the pages and the playing (the platform law).
  - The platform CANNOT SEARCH HEOS (no search feature; search_media answered HTTP 500). Search is the
    one thing this module does itself, through the player's own published interface (the HEOS CLI,
    TCP 1255) at the address the platform's own HEOS entry talks to. Justified rebuild: register 767.
  - DAVE'S RULING (768): a service shows only when it is signed in AND gives something to play when
    asked. The player's "available" flag is not proof — Tidal and Deezer are flagged available on
    Dave's Marantz, yet every search answers "System error" and every page comes back empty. So each
    service is MEASURED: its own page is read; one with nothing in it is not offered at all.

Same answers, same shapes, as speakermusic.py (Sonos), so the pages and Assist need no HEOS branch.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse

from proos.speakermusic import SpeakerMusicError

CLI_PORT = 1255
CLI_TIMEOUT_S = 12      # plumbing: how long one CLI command may take before it is called failed
WORKS_KEPT_S = 600      # plumbing: how long one measurement of "this service gives something to play" is kept
PLAY_WATCH_S = 15.0     # plumbing: how long a play is watched for the player to say playing (9.6 s seen, 766)
BROWSE_ROOT = "heos://media"

_LOCK = threading.Lock()
_WORKS = {}             # (host, sid) -> (gives something to play, when measured, why not)
_CRITERIA = {}          # (host, sid) -> the service's own search kinds, as the player lists them


class HeosSpeaker:
    """One HEOS player: its entity on the platform, its HEOS player id, the address the platform's HEOS
    entry talks to, and the platform client."""

    def __init__(self, client, entity_id, pid, host):
        self.client, self.entity_id, self.pid, self.host = client, entity_id, pid, host
        self.household_id = "heos:%s" % host
        self.ip_address = host
        self.player_name = entity_id


PLAYER_ID_TEMPLATE = (
    "{%- for i in device_attr(device_id(eid), 'identifiers') or [] -%}"
    "{%- if i[0] == 'heos' -%}{{ i[1] }}{%- endif -%}{%- endfor -%}"
)


def player_id(client, entity_id):
    """The HEOS player id — the platform's own device identifier for this player (read, never typed)."""
    try:
        tpl = "{% set eid = '" + str(entity_id).replace("'", "") + "' %}" + PLAYER_ID_TEMPLATE
        pid = str(client.render_template(tpl) or "").strip()
        return pid or None
    except Exception:                                            # noqa: BLE001
        return None


def cli_host():
    """The address the platform's own HEOS entry talks to."""
    from proos import platform_entries as _pe
    return _pe.host_of("heos") or None


def connect(client, entity_id):
    pid = player_id(client, entity_id)
    if not pid:
        raise SpeakerMusicError("the platform doesn't hold a HEOS player id for this speaker")
    host = cli_host()
    if not host:
        raise SpeakerMusicError("the platform's HEOS entry has no address")
    return HeosSpeaker(client, entity_id, pid, host)


# ── the player's own interface: only for search, the one thing the platform can't do ──────────
def _parse_msg(msg):
    return dict(urllib.parse.parse_qsl(str(msg or ""), keep_blank_values=True))


def cli(host, command, timeout=CLI_TIMEOUT_S, connect_to=None):
    """One HEOS CLI command on its own short connection → (fields of its message, payload). A refusal is
    SpeakerMusicError carrying the player's own words. connect_to: stand-in for tests."""
    sock = (connect_to or socket.create_connection)((host, CLI_PORT), timeout=5)
    try:
        sock.settimeout(1.0)
        sock.sendall(("heos://%s\r\n" % command).encode())
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            for line in buf.split(b"\r\n"):
                try:
                    d = json.loads(line)
                except Exception:                                # noqa: BLE001
                    continue
                h = d.get("heos") or {}
                if "command under process" in str(h.get("message", "")) or str(h.get("command", "")).startswith("event/"):
                    continue
                fields = _parse_msg(h.get("message"))
                if h.get("result") != "success":
                    raise SpeakerMusicError("the player says: %s" % (fields.get("text") or h.get("message") or "no reason given"))
                return fields, d.get("payload")
        raise SpeakerMusicError("the player didn't answer")
    finally:
        try:
            sock.close()
        except Exception:                                        # noqa: BLE001
            pass


def search_criteria(sp, sid):
    key = (sp.host, int(sid))
    if key not in _CRITERIA:
        _f, payload = cli(sp.host, "browse/get_search_criteria?sid=%d" % int(sid))
        _CRITERIA[key] = [{"name": c.get("name") or "", "scid": c.get("scid")} for c in payload or [] if c.get("scid") is not None]
    return _CRITERIA[key]


def media_uri(sid, item):
    """The platform's own address for one item from the player's search (the same form the platform's
    library writes — pyheos util/mediauri.to_media_uri), so the PLATFORM can play it."""
    container = str(item.get("container") or "").lower() == "yes"
    params = {"name": urllib.parse.unquote(str(item.get("name") or "")), "image_url": item.get("image_url") or "",
              "playable": str(str(item.get("playable") or "").lower() == "yes"), "browsable": str(container)}
    for k, v in (("container_id", "cid"), ("media_id", "mid"), ("artist", "artist"), ("album", "album"), ("album_id", "album_id")):
        if item.get(v):
            params[k] = item.get(v)
    return "%s/%d/%s?%s" % (BROWSE_ROOT, int(sid), item.get("type") or "song", urllib.parse.urlencode(params))


def search_card(sid, item):
    container = str(item.get("container") or "").lower() == "yes"
    return {"id": media_uri(sid, item), "title": urllib.parse.unquote(str(item.get("name") or "")),
            "artist": item.get("artist") or None, "art": item.get("image_url") or None,
            "type": item.get("type") or None, "open": container,
            "play": str(item.get("playable") or "").lower() == "yes"}


# ── the platform's own pages ──────────────────────────────────────────────────────────────────
def _browse(sp, mcid=None):
    """The platform's own browse of this player (its HEOS integration answers)."""
    from proos.ha_ws import ws_command
    f = {"entity_id": sp.entity_id}
    if mcid:
        f.update(media_content_id=mcid, media_content_type="")
    return ws_command(sp.client.base_url, sp.client._token, "media_player/browse_media", timeout=CLI_TIMEOUT_S, **f) or {}


def _path(mcid):
    """(source id, kind) from a platform HEOS address; (None, None) if it isn't one."""
    if not str(mcid or "").startswith(BROWSE_ROOT + "/"):
        return None, None
    p = urllib.parse.urlparse(mcid).path.strip("/").split("/")
    try:
        return int(p[0]), (p[1] if len(p) > 1 else "")
    except (ValueError, IndexError):
        return None, None


def card(node):
    return {"id": node.get("media_content_id"), "title": node.get("title") or "", "artist": None,
            "art": node.get("thumbnail") or None, "type": node.get("media_class") or None,
            "open": bool(node.get("can_expand")), "play": bool(node.get("can_play"))}


def sources(sp):
    """The player's music services the PLATFORM lists, signed in (the player's own flag). Not yet proof
    they work — works() measures that."""
    out = []
    for c in (_browse(sp) or {}).get("children") or []:
        mcid = c.get("media_content_id") or ""
        sid, kind = _path(mcid)
        if sid is None or kind != "music_service":
            continue
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(mcid).query))
        if q.get("available") != "True":
            continue
        out.append({"service_id": sid, "service": c.get("title") or q.get("name") or "", "root": mcid})
    return out


def _sections(sp, root, sections=12, per=12):
    """A service's own page: its first level in its own order; each folder with its first items."""
    kids = list(((_browse(sp, root) or {}).get("children") or []))[:sections]
    out = [card(k) for k in kids]
    def fill(i, k):
        if not k.get("can_expand"):
            return
        try:
            out[i]["items"] = [card(x) for x in list((_browse(sp, k.get("media_content_id")) or {}).get("children") or [])[:per]]
        except Exception as e:                                   # noqa: BLE001
            out[i]["items"], out[i]["error"] = [], str(e)[:160]
    ts = [threading.Thread(target=fill, args=(i, k), daemon=True) for i, k in enumerate(kids)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(CLI_TIMEOUT_S + 3)
    return out


def gives_something(secs):
    """Dave's test (768): there is something to play or open on the service's own page."""
    return any((s.get("items") or []) or (s.get("play") and not s.get("open")) for s in secs or [])


def first_title(secs):
    """The first title on a service's own page: a playable item's, else a section's."""
    for s in secs or []:
        for i in s.get("items") or []:
            if i.get("title"):
                return i["title"]
    return next((s.get("title") for s in secs or [] if s.get("title")), "")


def works(sp, src, now=None):
    """(True, None) when this service gives something to play, measured on its own page; else (False, why).
    Kept WORKS_KEPT_S, so a service that starts answering shows again by itself."""
    now = now or time.time()
    key = (sp.host, src["service_id"])
    kept = _WORKS.get(key)
    if kept and now - kept[1] < WORKS_KEPT_S:
        return kept[0], kept[2]
    try:
        secs = _sections(sp, src["root"], sections=4, per=3)
        ok = gives_something(secs)
        why = None if ok else "%s gave nothing to play on this player" % src["service"]
        # REGISTER 772 (seen on the box): Deezer's page lists a fixed "Flow" entry the player shows without
        # asking Deezer — and playing it left the Marantz idle. A listed item is not a played one. A
        # service that can be searched must also ANSWER a search, asked with the first title on its own
        # page (no word invented); its refusal hides it, in the player's words.
        if ok:
            crit = search_criteria(sp, src["service_id"])
            if crit:
                probe = first_title(secs)
                if probe:
                    cli(sp.host, "browse/search?sid=%d&search=%s&scid=%s&range=0,0"
                        % (int(src["service_id"]), urllib.parse.quote(probe[:128]), crit[0]["scid"]))
    except SpeakerMusicError as e:
        ok, why = False, "%s didn't answer a search on this player (%s)" % (src["service"], str(e)[:80])
    except Exception as e:                                       # noqa: BLE001
        ok, why = False, "%s didn't answer (%s)" % (src["service"], str(e)[:80])
    with _LOCK:
        _WORKS[key] = (ok, now, why)
    return ok, why


def services(sp):
    """The services to offer: signed in AND measured to give something to play (Dave, 768). HEOS keeps
    one sign-in per service, so each has one account. Those not offered are named, with why, for the log."""
    srcs = sources(sp)
    res = {}

    def one(s):
        res[s["service_id"]] = works(sp, s)
    ts = [threading.Thread(target=one, args=(s,), daemon=True) for s in srcs]
    for t in ts:
        t.start()
    for t in ts:
        t.join(CLI_TIMEOUT_S * 2 + 5)
    shown, hidden = [], []
    for s in srcs:
        ok, why = res.get(s["service_id"], (False, "%s didn't answer in time" % s["service"]))
        if ok:
            shown.append({"service_id": s["service_id"], "service": s["service"],
                          "accounts": [{"account": 0, "name": None}], "default": 0})
        else:
            hidden.append(why)
    return {"services": shown, "accounts": "the player's own sign-ins", "not_shown": hidden}


def _root(sp, service_id):
    src = next((s for s in sources(sp) if s["service_id"] == int(service_id)), None)
    if not src:
        raise SpeakerMusicError("that service isn't signed in on this player")
    return src


def home(sp, service_id, account=None, time_zone=None, sections=12, per=12):
    src = _root(sp, service_id)
    return {"service_id": int(service_id), "account": 0, "sections": _sections(sp, src["root"], sections, per)}


def open_item(sp, service_id, item_id, account=None, time_zone=None, index=0, count=50):
    kids = [card(x) for x in ((_browse(sp, item_id) or {}).get("children") or [])]
    return {"items": kids[int(index):int(index) + int(count)], "index": int(index), "total": len(kids)}


def search(sp, service_id, q, account=None, time_zone=None, categories=None, count=8):
    """The service's own search, in its own kinds, through the player (the platform has none)."""
    sid = int(service_id)
    crit = search_criteria(sp, sid)
    if not crit:
        raise SpeakerMusicError("this service can't be searched on the player")
    term = urllib.parse.quote(str(q or "").strip()[:128])
    results = []
    for c in crit:
        if categories and c["name"] not in categories:
            continue
        try:
            _f, payload = cli(sp.host, "browse/search?sid=%d&search=%s&scid=%s&range=0,%d" % (sid, term, c["scid"], max(int(count) - 1, 0)))
            results.append({"category": c["name"], "items": [search_card(sid, it) for it in payload or []]})
        except SpeakerMusicError as e:
            results.append({"category": c["name"], "items": [], "error": str(e)[:160]})
    return {"q": q, "results": results}


def item_name(item_id):
    try:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(item_id).query)).get("name") or ""
    except Exception:                                            # noqa: BLE001
        return ""


def _media_id(item_id):
    try:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(item_id).query)).get("media_id") or ""
    except Exception:                                            # noqa: BLE001
        return ""


_SEEN = ("media_content_id", "media_title", "media_station", "media_album_id")


def queue_length(sp):
    """How many items the player's own queue holds — the platform's own heos.get_queue."""
    r = sp.client._req("POST", "/api/services/heos/get_queue?return_response", {"entity_id": sp.entity_id}) or {}
    q = ((r.get("service_response") or {}).get(sp.entity_id) or {}).get("queue")
    return len(q) if isinstance(q, list) else None


def play(sp, service_id, item_id, account=None, time_zone=None, wait_s=PLAY_WATCH_S, limit=100, mode="now"):
    """Play through the PLATFORM's own play (proven, 767). The answer is the player's own state: playing,
    and showing something new (or already this very station).
    REGISTER 785: mode "next"/"add" → the platform's own play with enqueue next/add; the answer is the player's
    own queue, counted before and after."""
    c = sp.client
    if mode in ("next", "add"):
        _sid, kind = _path(item_id)
        if kind == "station":
            raise SpeakerMusicError("a station can't go in the queue — play it instead")
        before_n = queue_length(sp)
        # DAVE'S RULING B (12 Oct 11:41): "next" while a STATION plays (it never ends) plays it now — the platform's
        # own enqueue "play" (add to the queue and play it); the player says what it's playing in media_type.
        st = c._req("GET", "/api/states/%s" % sp.entity_id) or {}
        on_station = (st.get("attributes") or {}).get("media_type") == "station"
        enq = "play" if (mode == "next" and on_station) else mode
        c.call_service("media_player", "play_media", sp.entity_id,
                       {"media_content_id": item_id, "media_content_type": "music", "enqueue": enq})
        after_n = None
        for _ in range(10):
            after_n = queue_length(sp)
            if before_n is not None and after_n is not None and after_n > before_n:
                break
            time.sleep(0.5)
        grew = before_n is not None and after_n is not None and after_n > before_n
        return {"ok": grew, "mode": mode, "title": item_name(item_id), "queued": (after_n - before_n) if grew else 0,
                "on_queue": not on_station, "position": None, "started": enq == "play"}
    path = "/api/states/%s" % sp.entity_id
    before = c._req("GET", path) or {}
    b_attrs = before.get("attributes") or {}
    mid = _media_id(item_id)
    _sid, kind = _path(item_id)
    data = {"media_content_id": item_id, "media_content_type": "music"}
    if kind and kind != "station":
        data["enqueue"] = "replace"
    t0 = time.time()
    c.call_service("media_player", "play_media", sp.entity_id, data)
    state = ""
    while time.time() - t0 < wait_s:
        st = c._req("GET", path) or {}
        a = st.get("attributes") or {}
        state = st.get("state") or ""
        if state == "playing" and ((mid and a.get("media_album_id") == mid) or
                                   any(a.get(k) != b_attrs.get(k) for k in _SEEN) or before.get("state") != "playing"):
            break
        time.sleep(0.5)
    return {"ok": state == "playing", "state": state.upper(), "seconds": round(time.time() - t0, 1),
            "title": item_name(item_id), "queued": 1}


def assist_service(sp, attrs):
    """(service id, account, name) Assist searches when nobody named a service: the one the player is
    playing now (its own source id) if it can be searched, else the first offered that can. None if none."""
    offered = services(sp)["services"]
    searchable = []
    for s in offered:
        try:
            if search_criteria(sp, s["service_id"]):
                searchable.append(s)
        except SpeakerMusicError:
            continue
    if not searchable:
        return None
    now = next((s for s in searchable if str(s["service_id"]) == str((attrs or {}).get("media_source_id"))), None)
    s = now or searchable[0]
    return s["service_id"], None, s["service"]


# THE PLAYER'S OWN QUEUE (register 790) — all through the platform's own HEOS services: heos.get_queue,
# heos.remove_from_queue, heos.move_queue_item, and play_media with type "queue". An item's id is the player's own
# queue id.
def _queue_raw(sp):
    r = sp.client._req("POST", "/api/services/heos/get_queue?return_response", {"entity_id": sp.entity_id}) or {}
    q = ((r.get("service_response") or {}).get(sp.entity_id) or {}).get("queue")
    return q if isinstance(q, list) else []


def queue(sp):
    st = sp.client._req("GET", "/api/states/%s" % sp.entity_id) or {}
    cur_qid = (st.get("attributes") or {}).get("media_queue_id")
    items, cur = [], None
    for n, it in enumerate(_queue_raw(sp)):
        if str(it.get("queue_id")) == str(cur_qid):
            cur = n
        items.append({"queue_item_id": "q:%s" % it.get("queue_id"), "name": it.get("song") or "",
                      "artist": it.get("artist") or "", "image": it.get("image_url") or None})
    return {"items": items, "current_index": cur, "on_queue": (st.get("attributes") or {}).get("media_type") != "station",
            "can_move": True, "can_remove": True}


def _qid(item):
    s = str(item or "")
    if not s.startswith("q:"):
        raise SpeakerMusicError("that isn't an item in this queue")
    return int(s[2:])


def queue_remove(sp, item):
    sp.client._req("POST", "/api/services/heos/remove_from_queue", {"entity_id": sp.entity_id, "queue_ids": [_qid(item)]})
    return {"ok": True}


def queue_move(sp, item, shift):
    qid = _qid(item)
    ids = [int(it.get("queue_id")) for it in _queue_raw(sp)]
    if qid not in ids:
        raise SpeakerMusicError("that item has left the queue")
    dest = max(1, min(len(ids), ids.index(qid) + 1 + int(shift or 0)))
    sp.client._req("POST", "/api/services/heos/move_queue_item",
                   {"entity_id": sp.entity_id, "queue_ids": [qid], "destination_position": dest})
    return {"ok": True}


def queue_play(sp, item):
    sp.client.call_service("media_player", "play_media", sp.entity_id,
                           {"media_content_id": str(_qid(item)), "media_content_type": "queue"})
    return {"ok": True}
