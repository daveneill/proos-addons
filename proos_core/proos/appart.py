"""
ProOS Core -- product-level app artwork service.

WHITE-LABEL PRINCIPLE: app artwork is a PRODUCT asset, not an install chore.
Nothing here requires an installer to do anything on site.

Two layers, served to every dashboard/remote/widget identically:

  1. TILE PACK  (/app/appart/<slug>.png — SHIPPED WITH THE ADD-ON)
     The exact home-screen tile artwork, populated ONCE in the ProOS release
     repo (appart/ folder). Every project, every install, every device gets
     the identical correct tile automatically with each Core release.

  2. ICON CACHE  (/data/appart_sq/<slug>.png — AUTOMATIC, PER INSTALL)
     Real square app icons fetched server-side from Apple's public catalogue
     the first time any surface asks, then cached forever. No client fetching,
     no per-device caches — one uniform source per home.

slug = app name, lowercase, non-alphanumerics collapsed to '_'
(Disney+ → disney, ABC iview → abc_iview, 7plus → 7plus).
"""
from __future__ import annotations
import json
import os
import re
import time
import urllib.parse
import urllib.request

_BUNDLED = "/app/appart"
_CACHE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "appart_sq")
_LOGOS = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "appart_logo")
_MISS = os.path.join(_CACHE, "_miss.json")
_MISS_TTL = 7 * 86400   # retry a failed lookup after a week
_UA = "ProOS/1.0 (smart-home dashboard; brand tile display)"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def tile_path(s: str):
    """Bundled product tile (exact artwork) or None."""
    s = slug(s)
    if not s:
        return None
    p = os.path.join(_BUNDLED, s + ".png")
    return p if os.path.isfile(p) else None


def _misses() -> dict:
    try:
        with open(_MISS, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _mark_miss(s: str) -> None:
    try:
        os.makedirs(_CACHE, exist_ok=True)
        d = _misses()
        d[s] = int(time.time())
        with open(_MISS, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    except Exception:
        pass


def _wm_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


def logo_path(name: str):
    """The brand's GENUINE logo file, resolved automatically from Wikimedia
    Commons (the canonical public home of brand logo artwork — real files, not
    AI redraws, which mangle trademarks). Cached forever; the dashboard
    composites it on the brand colour to make the tile. None when unresolved."""
    s = slug(name)
    if not s:
        return None
    for ext in ("svg", "png"):
        p = os.path.join(_LOGOS, s + "." + ext)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    key = "logo:" + s
    m = _misses().get(key)
    if m and (time.time() - m) < _MISS_TTL:
        return None
    try:
        q = urllib.parse.quote('"%s" logo' % (name or s))
        d = _wm_json("https://commons.wikimedia.org/w/api.php?action=query&list=search"
                     "&srnamespace=6&srlimit=8&format=json&srsearch=" + q)
        hits = ((d.get("query") or {}).get("search") or [])
        # Prefer an SVG whose title mentions 'logo'; PNG as second choice.
        title = None
        for want_ext in (".svg", ".png"):
            for h in hits:
                t = h.get("title") or ""
                if t.lower().endswith(want_ext) and "logo" in t.lower():
                    title = t
                    break
            if title:
                break
        if not title:
            _mark_miss(key)
            return None
        d = _wm_json("https://commons.wikimedia.org/w/api.php?action=query&prop=imageinfo"
                     "&iiprop=url&format=json&titles=" + urllib.parse.quote(title))
        pages = ((d.get("query") or {}).get("pages") or {})
        url = None
        for pg in pages.values():
            ii = (pg.get("imageinfo") or [{}])[0]
            url = ii.get("url")
            if url:
                break
        if not url:
            _mark_miss(key)
            return None
        ext = "svg" if url.lower().endswith(".svg") else "png"
        os.makedirs(_LOGOS, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(3_000_000)
        p = os.path.join(_LOGOS, s + "." + ext)
        with open(p, "wb") as fh:
            fh.write(data)
        return p
    except Exception:
        _mark_miss(key)
        return None


def sq_path(name: str):
    """Cached real square icon; fetched server-side (Apple catalogue) on first
    request, then served locally forever. None when genuinely unavailable."""
    s = slug(name)
    if not s:
        return None
    p = os.path.join(_CACHE, s + ".png")
    if os.path.isfile(p) and os.path.getsize(p) > 0:
        return p
    m = _misses().get(s)
    if m and (time.time() - m) < _MISS_TTL:
        return None
    url = None
    try:
        q = urllib.parse.quote(name or s)
        req = urllib.request.Request(
            "https://itunes.apple.com/search?country=au&limit=1&media=software&entity=software&term=" + q,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8"))
        it = (d.get("results") or [{}])[0]
        a = it.get("artworkUrl100") or ""
        if a:
            url = a.replace("100x100bb", "512x512bb")
    except Exception:
        url = None
    if not url:
        _mark_miss(s)
        return None
    try:
        os.makedirs(_CACHE, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(3_000_000)
        with open(p, "wb") as fh:
            fh.write(data)
        return p
    except Exception:
        _mark_miss(s)
        return None
