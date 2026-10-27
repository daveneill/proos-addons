"""APPLE'S OWN TV CATALOGUE FOR THE APPLE TV PAGE'S SEARCH (register 1036; Dave 27 Oct 15:49: "its already on the Apple
TV if you are subscribed you see it").

The Apple TV app's catalogue is what Apple's own web player (tv.apple.com) reads: titles, posters, and which apps carry
each title in a country, with the link that opens it in that app. Core reads it the same way and hands the page plain
data. Nothing here is a guess: a field Apple does not return is left out, and a catalogue that cannot be reached says so
(the page then still types on the TV).

  search(q)          → {"results": [{id, kind, title, year, poster, description}], "source": "apple_tv_app"}
  title(kind, id)    → {"where": [{name, bundle, url}]}  — the apps Apple lists for that title here, with their links

The web player's public parameters are kept in one place (PARAMS); Apple can change them. When the catalogue answers
with an error, the error is returned in words and the page shows it.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

BASE = "https://uts-api.itunes.apple.com/uts/v3"
# PLUMBING: the public query the Apple TV web player itself sends (locale and storefront for Australia).
PARAMS = {"caller": "web", "locale": "en-AU", "pfm": "web", "sf": "143460", "v": "88",
          "utscf": "OjAAAAEAAAAAAAMAEAAAACMAKwAuAA~~", "utsk": "6e3013c6d6fae3c2::::::9318c17fb39d6b9c"}
TIMEOUT_S = 6.0
CACHE_S = 600
_cache: dict = {}
_lock = threading.Lock()


def _get(path: str, extra: dict) -> dict:
    q = dict(PARAMS)
    q.update(extra or {})
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    with _lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ProOS", "Accept": "application/json",
                                               "Origin": "https://tv.apple.com", "Referer": "https://tv.apple.com/"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    with _lock:
        _cache[url] = (time.time(), data)
        if len(_cache) > 200:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
                _cache.pop(k, None)
    return data


def _img(images: dict, w=300, h=450) -> str | None:
    for key in ("coverArt", "posterArt", "fullColorContentLogo", "previewFrame", "shelfItemImage"):
        im = (images or {}).get(key)
        if isinstance(im, dict) and im.get("url"):
            ratio = (im.get("height") or h) / float(im.get("width") or w)
            return (im["url"].replace("{w}", str(w)).replace("{h}", str(int(w * ratio)))
                    .replace("{f}", "jpg").replace("{c}", ""))
    return None


def _items(node, out: list, seen: set) -> None:
    """Every catalogue item in a search answer, wherever Apple put it (shelves change shape)."""
    if isinstance(node, dict):
        t = node.get("type")
        if t in ("Movie", "Show") and node.get("id") and node.get("title") and node["id"] not in seen:
            seen.add(node["id"])
            out.append(node)
            return
        for v in node.values():
            _items(v, out, seen)
    elif isinstance(node, list):
        for v in node:
            _items(v, out, seen)


def parse_search(data: dict) -> list:
    """Pure: an Apple TV app search answer → plain results."""
    raw: list = []
    _items((data or {}).get("data"), raw, set())
    out = []
    for it in raw[:24]:
        rd = it.get("releaseDate")
        year = None
        if isinstance(rd, (int, float)):
            year = time.gmtime(rd / 1000.0).tm_year
        out.append({"id": it["id"], "kind": "movie" if it.get("type") == "Movie" else "show",
                    "title": it.get("title"), "year": year, "poster": _img(it.get("images") or {}),
                    "description": it.get("description")})
    return out


def parse_where(data: dict) -> list:
    """Pure: an Apple TV app title answer → the apps Apple lists for it, each with its link and app id."""
    d = (data or {}).get("data") or {}
    channels = d.get("channels") or {}
    seen, out = set(), []
    for p in (d.get("playables") or {}).values() if isinstance(d.get("playables"), dict) else (d.get("playables") or []):
        ch = channels.get(p.get("channelId")) or {}
        name = ch.get("name") or p.get("channelId")
        if not name or name in seen:
            continue
        seen.add(name)
        bundle = None
        ids = ch.get("appBundleIds") or []
        if ids:
            bundle = ids[0]
        url = ((p.get("punchoutUrls") or {}).get("open") or (p.get("punchoutUrls") or {}).get("play")
               or p.get("url") or None)
        out.append({"name": name, "bundle": bundle, "url": url})
    return out


def search(q: str) -> dict:
    q = (q or "").strip()[:120]
    if not q:
        return {"results": []}
    try:
        return {"results": parse_search(_get("/search", {"searchTerm": q})), "source": "apple_tv_app"}
    except Exception as err:                                 # noqa: BLE001
        return {"error": "Apple's catalogue did not answer (%s). Typing on the TV still works." % type(err).__name__}


def title(kind: str, item_id: str) -> dict:
    path = "/movies/" if kind == "movie" else "/shows/"
    try:
        return {"where": parse_where(_get(path + urllib.parse.quote(str(item_id)), {}))}
    except Exception as err:                                 # noqa: BLE001
        return {"where": [], "error": "Apple's catalogue did not answer (%s)" % type(err).__name__}
