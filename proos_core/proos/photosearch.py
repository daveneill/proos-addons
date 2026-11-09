"""
ProOS Core — real photo search for backgrounds (areas, the Home page, scenes). Registers 1281, 1282.

Dave, 10 Nov 2026 08:33: the picture "needs to be a search for anything not just a home … high quality images … if you
typed Aston Martin then it will give you high quality images of an aston martin"; 08:47 "all option added as long as the
UnSplash or Pexels there is no watermarks etc". 10:15 Pexels has paused new API keys; 10:24 Dave holds an Unsplash demo key
(50 requests an hour) — so the source is UNSPLASH (1282).

UNSPLASH'S RULES, KEPT HERE (help.unsplash.com/api-guidelines, unsplash.com/documentation):
  - the key stays on the box; the pages ask Core (a proxy with one shared key — users never need an Unsplash account);
  - photos are shown from the urls the API returns (hotlinked; raw + size parameters is allowed);
  - when a person PICKS a photo, the photo's download_location is called (track_use) — it only counts the use;
  - each photo is credited "Photo by <photographer> on Unsplash", linking the photographer and Unsplash with
    ?utm_source=<app>&utm_medium=referral.
Nothing is stored: a picked photo is used by its Unsplash address, like the curated set.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.unsplash.com/search/photos"
_TRACK_PREFIX = "https://api.unsplash.com/photos/"       # the only address track_use will ever call
_UTM = "utm_source=ProOS&utm_medium=referral"
_TIMEOUT = 12
SOURCE = {"name": "Unsplash", "url": "https://unsplash.com/?" + _UTM}


def _utm(u: str) -> str:
    return (u + ("&" if "?" in u else "?") + _UTM) if u else ""


def _headers(key):
    return {"Authorization": "Client-ID " + key.strip(), "Accept-Version": "v1", "User-Agent": "ProOS"}


def _fail(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return {"error": "Unsplash refused the key — check the Access Key in Pro › Tech › Assist.", "needs_key": True}
        if e.code in (403, 429):
            return {"error": "Unsplash's hourly limit is used up (50 an hour on a demo key) — try again later."}
        return {"error": "Unsplash didn't answer (%s)." % e.code}
    return {"error": "Couldn't reach Unsplash — check the box's internet."}


def search(query: str, key: str, per_page: int = 15, opener=None) -> dict:
    """{"photos": [{thumb, url, credit, credit_url, link, alt, track}], "source": {name, url}} or {"error": plain words}."""
    q = (query or "").strip()
    if not q:
        return {"error": "Type what you are looking for first."}
    if not (key or "").strip():
        return {"error": "Photo search needs an Unsplash Access Key — add it in Pro › Tech › Assist.", "needs_key": True}
    url = API + "?" + urllib.parse.urlencode({"query": q, "per_page": max(1, min(int(per_page or 15), 30)),
                                             "orientation": "landscape", "content_filter": "high"})
    try:
        with (opener or urllib.request.urlopen)(urllib.request.Request(url, headers=_headers(key)), timeout=_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        return _fail(e)
    out = []
    for p in data.get("results") or []:
        urls, user, links = p.get("urls") or {}, p.get("user") or {}, p.get("links") or {}
        raw = urls.get("raw")
        big = (raw + ("&" if "?" in raw else "?") + "w=2400&q=80&fm=jpg&fit=max") if raw else (urls.get("full") or urls.get("regular"))
        if not big:
            continue
        out.append({"thumb": urls.get("small") or urls.get("regular") or big, "url": big,
                    "credit": user.get("name") or "", "credit_url": _utm((user.get("links") or {}).get("html") or ""),
                    "link": _utm(links.get("html") or ""), "alt": p.get("alt_description") or p.get("description") or "",
                    "track": links.get("download_location") or ""})
    return {"photos": out, "source": dict(SOURCE)}


def track_use(download_location: str, key: str, opener=None) -> dict:
    """Unsplash's rule: tell it a photo was picked. Only ever calls Unsplash's own photos address."""
    loc = (download_location or "").strip()
    if not loc.startswith(_TRACK_PREFIX):
        return {"error": "not an Unsplash photo"}
    if not (key or "").strip():
        return {"error": "no key"}
    try:
        with (opener or urllib.request.urlopen)(urllib.request.Request(loc, headers=_headers(key)), timeout=_TIMEOUT) as r:
            r.read()
        return {"ok": True}
    except Exception as e:
        return _fail(e)
