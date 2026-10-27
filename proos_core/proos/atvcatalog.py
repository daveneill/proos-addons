"""APPLE'S OWN TV CATALOGUE FOR THE APPLE TV PAGE'S SEARCH (register 1036; Dave 27 Oct 15:49: "its already on the Apple
TV if you are subscribed you see it").

The Apple TV app's catalogue is what Apple's own web player (tv.apple.com) reads: titles, posters, and which apps carry
each title in a country, with the link that opens it in that app. Core reads it the same way and hands the page plain
data. Nothing here is a guess: a field Apple does not return is left out, and a catalogue that cannot be reached says so
(the page then still types on the TV).

  search(q)          → {"results": [{id, kind, title, year, poster, description}], "source": "apple_tv_app"}
  title(kind, id)    → {"where": [{name, bundle, url}]}  — the apps Apple lists for that title here, with their links

The parameters are Apple's own, read from its public /configurations answer each time they expire; nothing is typed in
here. When the catalogue answers with an error, the error is returned in words and the page shows it.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://tv.apple.com/api/uts/v3"
# What Apple's own TV web player sends, read from Apple at run time — never typed in here. /configurations needs no key
# and answers with the parameters every other request needs (requiredParamsMap.Default). Measured 27 Oct from
# tv.apple.com: the web platform ("pfm": "web") lists only Apple's own titles; asking as an Apple TV does
# ("pfm": "appletv") lists the full catalogue with every app that carries a title here (Binge, Prime Video, ABC iview…).
CONFIG_QUERY = {"caller": "web", "locale": "en-AU", "pfm": "web", "sf": "143460", "v": "100"}
AS_APPLE_TV = {"pfm": "appletv", "locale": "en-AU", "sf": "143460"}
ITUNES_BUNDLE = "com.apple.MobileStore"
TV_APP_BUNDLE = "com.apple.TVWatchList"
BUY_OR_RENT = "Apple TV · Buy or Rent"
TIMEOUT_S = 6.0
CACHE_S = 600
EPISODE_PAGE = 100
CONFIG_S = 6 * 3600
_cache: dict = {}
_conf: dict = {"at": 0.0, "params": None}
_lock = threading.Lock()


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ProOS", "Accept": "application/json",
                                               "Origin": "https://tv.apple.com", "Referer": "https://tv.apple.com/"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _params(refresh: bool = False) -> dict:
    with _lock:
        if not refresh and _conf["params"] and time.time() - _conf["at"] < CONFIG_S:
            return dict(_conf["params"])
    data = _fetch(BASE + "/configurations?" + urllib.parse.urlencode(CONFIG_QUERY))
    req = (((data or {}).get("data") or {}).get("applicationProps") or {}).get("requiredParamsMap") or {}
    params = dict(req.get("Default") or req.get("WithoutUtsk") or {})
    if not params:
        raise ValueError("Apple's catalogue gave no parameters")
    params.update(AS_APPLE_TV)
    with _lock:
        _conf.update({"at": time.time(), "params": params})
    return dict(params)


def _get(path: str, extra: dict) -> dict:
    q = _params()
    q.update(extra or {})
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    with _lock:
        hit = _cache.get(path + repr(sorted((extra or {}).items())))
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    try:
        data = _fetch(url)
    except urllib.error.HTTPError as err:
        if err.code not in (400, 401, 403):
            raise
        q = _params(refresh=True)                           # Apple moved its parameters: read them again, once
        q.update(extra or {})
        data = _fetch(BASE + path + "?" + urllib.parse.urlencode(q))
    with _lock:
        _cache[path + repr(sorted((extra or {}).items()))] = (time.time(), data)
        if len(_cache) > 200:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
                _cache.pop(k, None)
    return data


def _img(images: dict, w=300, h=450) -> str | None:
    for key in ("shelfItemImage", "posterArt", "coverArt"):
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
    """Pure: an Apple TV app title answer → the apps Apple lists for it here, once each, each with its app id and the
    link that opens the title in that app (Apple's own "open" link first, then its web link)."""
    d = (data or {}).get("data") or {}
    channels = d.get("channels") or {}
    pl = d.get("playables") or {}
    seen, out = set(), []
    for p in (pl.values() if isinstance(pl, dict) else pl):
        ch = channels.get(p.get("channelId")) or {}
        name = ch.get("name") or p.get("serviceName") or p.get("channelId")
        if not name or name in seen:
            continue
        seen.add(name)
        ids = ch.get("appBundleIds") or []
        po = p.get("punchoutUrls") or {}
        bundle = ids[0] if ids else None
        url = po.get("open") or po.get("play") or p.get("url") or None
        if bundle == ITUNES_BUNDLE or name == "iTunes":
            # Dave 27 Oct: "iTunes … no longer exists" — buying and renting is in the Apple TV app on the Apple TV, so the
            # entry is named for where it opens, with Apple's own tv.apple.com link (register 1037)
            name, bundle, url = BUY_OR_RENT, TV_APP_BUNDLE, (p.get("url") or url)
            if name in seen:
                continue
            seen.add(name)
        out.append({"name": name, "bundle": bundle, "url": url})
    return out


def parse_description(data: dict):
    c = (((data or {}).get("data") or {}).get("content") or {})
    return c.get("description") or None


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
        data = _get(path + urllib.parse.quote(str(item_id)), {})
        return {"where": parse_where(data), "description": parse_description(data)}
    except Exception as err:                                 # noqa: BLE001
        return {"where": [], "error": "Apple's catalogue did not answer (%s)" % type(err).__name__}


def _norm(t) -> str:
    return " ".join(str(t or "").lower().replace("’", "'").split())


def _found(data: dict, kind: str) -> list:
    raw: list = []
    _items((data or {}).get("data"), raw, set())
    return [r for r in raw if r.get("type") == kind]


def _wide(images: dict, w=1600) -> str | None:
    for key in ("contentImage", "posterArt", "contentImage16X9", "previewFrame"):
        im = (images or {}).get(key)
        if isinstance(im, dict) and im.get("url") and (im.get("width") or 0) >= (im.get("height") or 0):
            h = int(w * (im.get("height") or 9) / float(im.get("width") or 16))
            return im["url"].replace("{w}", str(w)).replace("{h}", str(h)).replace("{f}", "jpg").replace("{c}", "")
    return None


def match_episode(episodes: list, title: str, season=None, episode=None):
    """Pure: the ONE episode whose title is exactly the one the Apple TV reported (and whose season / episode agree
    when the Apple TV gave them). Two with the same title and nothing to tell them apart → no match, never a pick."""
    cand = [e for e in episodes if _norm(e.get("title")) == _norm(title)]
    if season is not None:
        cand = [e for e in cand if str(e.get("seasonNumber")) == str(season)]
    if episode is not None:
        cand = [e for e in cand if str(e.get("episodeNumber")) == str(episode)]
    return cand[0] if len(cand) == 1 else None


def now_playing(series: str, title: str, season=None, episode=None) -> dict:
    """What Apple's catalogue says about what is playing — ONLY on an exact match (register 1037; Dave 27 Oct: no episode
    info, low-quality art). A show: the series by its exact name, then the episode by its exact title. A film: its exact
    title. No exact, single match → {"match": False} and the page shows only what the Apple TV said."""
    try:
        if series:
            shows = [r for r in _found(_get("/search", {"searchTerm": series}), "Show") if _norm(r.get("title")) == _norm(series)]
            if len(shows) != 1:
                return {"match": False, "why": "no single show named exactly %r" % series}
            sid = shows[0]["id"]
            eps, start, total = [], 0, None
            while total is None or start < total:
                d = (_get("/shows/%s/episodes" % urllib.parse.quote(sid), {"nextToken": "%d:%d" % (start, EPISODE_PAGE)}) or {}).get("data") or {}
                page = d.get("episodes") or []
                total = int(d.get("totalEpisodeCount") or len(page))
                eps += page
                if not page or start > 2000:
                    break
                start += EPISODE_PAGE
            ep = match_episode(eps, title, season, episode)
            if not ep:
                return {"match": False, "why": "no single episode titled exactly %r" % title}
            return {"match": True, "kind": "episode", "season": ep.get("seasonNumber"), "episode": ep.get("episodeNumber"),
                    "description": ep.get("description"), "image": _wide(ep.get("images") or {}),
                    "show": shows[0].get("title")}
        films = [r for r in _found(_get("/search", {"searchTerm": title}), "Movie") if _norm(r.get("title")) == _norm(title)]
        if len(films) != 1:
            return {"match": False, "why": "no single film named exactly %r" % title}
        d = (_get("/movies/%s" % urllib.parse.quote(films[0]["id"]), {}) or {}).get("data") or {}
        c = d.get("content") or {}
        return {"match": True, "kind": "movie", "description": c.get("description"),
                "image": _wide(c.get("images") or films[0].get("images") or {})}
    except Exception as err:                                 # noqa: BLE001
        return {"match": False, "why": "Apple's catalogue did not answer (%s)" % type(err).__name__}
