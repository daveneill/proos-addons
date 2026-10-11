"""APPLE MUSIC'S OWN CATALOGUE, FOR THE HOME'S SONOS — music design stage 4 (register 759).

Proven from Dave's Mac on 10 Oct (register 742): Apple's official catalogue search with the Protech
MusicKit key answers in 0.5–1.4 s, and the Sonos plays an Apple song by its catalogue id with the
speaker's OWN Apple sign-in (PLAYING 4.8 s). The household's stored Apple sign-in is refused to outside
apps (register 740), so the speaker can't be asked to search Apple — Apple's catalogue is.

THE KEY NEVER COMES HERE (Dave's ruling 3, 11 Oct: "installer pasted now, ProOS Connect later"): the
installer makes a token from the Protech key on their own Mac and pastes the TOKEN into Pro. A token
expires (Apple allows up to six months); Pro shows when. Only the token is stored, and it is never sent
back out of Core — the status says whose key and when it ends, never the token.

Search, charts and a playlist's or album's tracks come from Apple; what plays is the speaker, song by
song, with the home's own Apple account (speakermusic.py does the playing).
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.music.apple.com/v1"
TOKEN_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "apple_music_token.json")
TIMEOUT_S = 8          # plumbing: how long one catalogue request may take before it is called failed


class AppleMusicError(Exception):
    """A reason, in plain words, Apple's catalogue can't be used. Shown as it is."""


def _claims(token):
    """The token's own header and claims (read, not verified — Apple verifies). None if not a token."""
    try:
        h, p = str(token).split(".")[:2]
        dec = lambda s: json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8"))  # noqa: E731
        return dec(h), dec(p)
    except Exception:                                            # noqa: BLE001
        return None


def save_token(token, now=None):
    """Store an installer-pasted token after reading what it says. Returns the status (never the token)."""
    token = "".join(str(token or "").split())
    c = _claims(token)
    if not c:
        raise AppleMusicError("that isn't an Apple Music token — paste the whole token the Mac made")
    head, body = c
    exp = body.get("exp")
    if not isinstance(exp, (int, float)) or exp <= (now or time.time()):
        raise AppleMusicError("that token has already expired — make a new one on the Mac")
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"token": token, "saved_at": int(now or time.time())}, fh)
    os.replace(tmp, TOKEN_PATH)
    return status(now)


def clear_token():
    try:
        os.remove(TOKEN_PATH)
    except FileNotFoundError:
        pass
    return status()


def _token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("token") or None
    except Exception:                                            # noqa: BLE001
        return None


def status(now=None):
    """What Pro shows: set or not, whose key, when it ends. Never the token itself."""
    tok = _token()
    c = _claims(tok) if tok else None
    if not c:
        return {"set": False}
    head, body = c
    exp = int(body.get("exp") or 0)
    left = (exp - (now or time.time())) / 86400.0
    return {"set": True, "key_id": head.get("kid"), "team_id": body.get("iss"),
            "expires": time.strftime("%Y-%m-%d", time.localtime(exp)),
            "days_left": round(left, 1), "expired": left <= 0}


def ready():
    s = status()
    return bool(s.get("set") and not s.get("expired"))


def storefront(client):
    """Which Apple store to search: the home's own country as the platform holds it."""
    try:
        cc = (client._req("GET", "/api/config") or {}).get("country") or ""
    except Exception:                                            # noqa: BLE001
        cc = ""
    return (str(cc).strip().lower() or "us")


def _get(path, params=None):
    tok = _token()
    if not tok:
        raise AppleMusicError("Apple Music search needs the installer's Apple token (Pro › Tech Tools › Assist)")
    url = API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AppleMusicError("Apple refused the token (HTTP %d) — it may have expired; make a new one" % e.code)
        raise AppleMusicError("Apple Music answered HTTP %d" % e.code)
    except Exception as e:                                       # noqa: BLE001
        raise AppleMusicError("Apple Music didn't answer (%s)" % type(e).__name__)


def _art(attrs):
    u = ((attrs or {}).get("artwork") or {}).get("url") or ""
    return u.replace("{w}", "300").replace("{h}", "300") or None


def card(obj):
    """One Apple item as the pages and Assist see it — the same shape as a speaker service's items."""
    kind = obj.get("type") or ""
    a = obj.get("attributes") or {}
    return {"id": "apple:%s:%s" % (kind, obj.get("id")), "title": a.get("name") or "",
            "artist": a.get("artistName") or a.get("curatorName") or None, "art": _art(a),
            "type": kind.rstrip("s") or None, "open": kind != "songs", "play": kind != "artists"}


SEARCH_TYPES = ("songs", "albums", "artists", "playlists")


def search(term, sf, limit=8):
    """Apple's own search, in Apple's own groups."""
    j = _get("/catalog/%s/search" % sf, {"term": term, "types": ",".join(SEARCH_TYPES), "limit": int(limit)})
    res = (j or {}).get("results") or {}
    return [{"category": t, "items": [card(o) for o in (res.get(t) or {}).get("data") or []]} for t in SEARCH_TYPES
            if (res.get(t) or {}).get("data")]


def charts(sf, limit=12):
    """Apple's own charts — the home page for Apple Music (the person's own For You needs their sign-in
    on their phone: stage 7)."""
    j = _get("/catalog/%s/charts" % sf, {"types": "playlists,albums,songs", "limit": int(limit)})
    out = []
    for kind, groups in ((j or {}).get("results") or {}).items():
        for g in groups or []:
            items = [card(o) for o in g.get("data") or []]
            if items:
                out.append({"id": "apple-chart:%s" % kind, "title": g.get("name") or kind.title(),
                            "open": True, "play": None, "items": items})
    return out


def split(item_id):
    """("songs"|"albums"|"playlists"|"artists", catalogue id) from an Apple item id; None if not one."""
    p = str(item_id or "").split(":", 2)
    return (p[1], p[2]) if len(p) == 3 and p[0] == "apple" else None


def tracks(item_id, sf, limit=100):
    """The songs an Apple item plays: a song itself; an album's or playlist's tracks; an artist's top songs."""
    k = split(item_id)
    if not k:
        raise AppleMusicError("that isn't an Apple Music item")
    kind, cid = k
    if kind == "songs":
        j = _get("/catalog/%s/songs/%s" % (sf, cid))
    elif kind == "artists":
        j = _get("/catalog/%s/artists/%s/view/top-songs" % (sf, cid), {"limit": min(int(limit), 20)})
    else:
        j = _get("/catalog/%s/%s/%s/tracks" % (sf, kind, cid), {"limit": min(int(limit), 100)})
    return [card(o) for o in (j or {}).get("data") or [] if o.get("type") == "songs"]
