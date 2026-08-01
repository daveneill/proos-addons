"""
ProOS Core -- app tile artwork: the ProOS-curated pack, shipped with the product.

ONE deterministic system, no scraping, no third-party lookups:
  * The release repo's  appart/  folder holds the tile graphics (see
    appart/README.md for the naming spec). They ship inside the add-on image
    at /app/appart and are served to every surface at
    /apps/art/tile/<slug>.png — identical on every install, offline, forever.
  * appart/aliases.json (optional) maps platform naming variants to one
    graphic: {"amazon_prime_video": "prime_video", ...} — because an Apple TV
    says "Prime Video" where another box says "Amazon Prime Video".
  * An app with no graphic renders as a clean neutral wordmark tile on the
    dashboard. Add the PNG centrally → next release carries it everywhere.

slug = app display name exactly as the device reports it, lowercased, every
run of non-alphanumerics collapsed to '_', trimmed:
  Netflix→netflix · Disney+→disney · ABC iview→abc_iview · 7plus→7plus
  Prime Video→prime_video · SBS On Demand→sbs_on_demand · F1 TV→f1_tv
"""
from __future__ import annotations
import base64
import io
import json
import os
import re
import zipfile

_BUNDLED = "/app/appart"                                              # ships with the release
_UPLOADED = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"),   # managed in Pro → Tech
                         "appart_tiles")                              # Tools; survives updates
                                                                      # AND factory reset (branding)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


# ── CANONICAL APP IDENTITY (spec, 1 Aug 2026) ────────────────────────────────
# One app = ONE display name + ONE tile, whatever the device calls it. A
# Samsung says "Spotify - Music and Podcasts", a Shield says "Spotify", an
# Apple TV says something else again — so the same app drew different labels
# (and sometimes different art) per device. This table folds every observed
# variant — name slugs AND package ids — onto one canonical identity.
#
# DISPLAY-ONLY by contract: launching always uses the device's own raw source
# string / package id (select_source accepts nothing else). Unknown apps pass
# through untouched — never invented, never renamed by guesswork.
#   canonical slug -> (display name, [alias slugs and package ids])
CANONICAL_APPS = {
    "netflix":       ("Netflix",       ["com.netflix.ninja"]),
    "disney":        ("Disney+",       ["disney_plus", "com.disney.disneyplus"]),
    "prime_video":   ("Prime Video",   ["amazon_prime_video",
                                        "com.amazon.amazonvideo.livingroom"]),
    "youtube":       ("YouTube",       ["com.google.android.youtube.tv"]),
    "youtube_kids":  ("YouTube Kids",  []),
    "spotify":       ("Spotify",       ["spotify_music_and_podcasts",
                                        "com.spotify.tv.android"]),
    "kayo":          ("Kayo",          ["kayo_sports",
                                        "au.com.kayosports.kayoapp"]),
    "binge":         ("Binge",         ["au.com.streamotion.binge"]),
    "stan":          ("Stan",          ["au.com.stan.and.tv"]),
    "plex":          ("Plex",          ["com.plexapp.android"]),
    "abc_iview":     ("ABC iview",     ["au.net.abc.iview"]),
    "sbs_on_demand": ("SBS On Demand", []),
    "7plus":         ("7plus",         []),
    "9now":          ("9Now",          []),
    "10":            ("10",            ["10_play", "ten_play"]),
    "foxtel":        ("Foxtel",        []),
    "apple_tv":      ("Apple TV",      ["apple_tv_plus"]),
    "tubi":          ("Tubi",          ["tubi_free_movies_tv"]),
    "channels":      ("Channels",      ["channels_dvr", "com.getchannels.dvr"]),
    "docplay":       ("DocPlay",       []),
    "calm":          ("Calm",          []),
    "animelab":      ("AnimeLab",      []),
    "paramount_plus": ("Paramount+",   ["paramount"]),
}

# WITHDRAWN same day (Dave, 2 Aug): "Live TV is still the tuner in the TV
# itself — Channels should just be seen as Apple TV." Live TV is a PLACE
# (the panel's own tuner, the watch_tv activity), never inferred from which
# app a source happens to run. Kept empty rather than deleted so the shape
# survives if a future product decision revisits it.
LIVE_TV_APPS = set()

_CANON_INDEX = None


def _canon_index() -> dict:
    """alias (slug or package id) -> (canon_slug, display_name, rank).
    rank = the table's own curated order (review §3e): tiles sort by it so
    every room's sheet feels deliberately arranged, unknown apps after."""
    global _CANON_INDEX
    if _CANON_INDEX is None:
        idx = {}
        for rank, (canon, (name, aliases)) in enumerate(CANONICAL_APPS.items()):
            idx[canon] = (canon, name, rank)
            idx[slug(name)] = (canon, name, rank)
            for a in aliases:
                idx[a] = (canon, name, rank)
                idx[slug(a)] = (canon, name, rank)
        _CANON_INDEX = idx
    return _CANON_INDEX


def canonical(name: str, package: str = ""):
    """{'name': display, 'slug': canon_slug} or None when unknown (caller
    keeps the raw name — fail-open, nothing is ever renamed by guesswork).
    Package id wins over the name: it is the device-independent identity."""
    idx = _canon_index()
    hit = idx.get((package or "").strip().lower()) or idx.get(slug(name))
    return {"name": hit[1], "slug": hit[0]} if hit else None


def identity_table() -> dict:
    """Flat alias -> {'name', 'slug', 'rank'} map for the surfaces (GET
    /apps/identity). Dashboards resolve locally with zero further calls."""
    return {a: {"name": n, "slug": c, "rank": r}
            for a, (c, n, r) in _canon_index().items()}


def _aliases() -> dict:
    try:
        with open(os.path.join(_BUNDLED, "aliases.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            return {}
        # Keys beginning with _ are section headings / notes, not aliases.
        return {k: v for k, v in d.items()
                if isinstance(v, str) and not k.startswith("_")}
    except Exception:
        return {}


_HIDDEN = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "appart_hidden.json")
_ORIGIN = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "appart_origin.json")


def _origins() -> dict:
    """Where each stored tile came from: 'fetched' (looked up automatically) or
    'uploaded' (supplied by a tech). Two states, and only two — a tile is
    either the one ProOS found or the one someone deliberately put there."""
    try:
        with open(_ORIGIN) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _set_origin(s: str, origin: str) -> None:
    d = _origins()
    d[s] = "fetched" if origin == "fetched" else "uploaded"
    try:
        os.makedirs(os.path.dirname(_ORIGIN), exist_ok=True)
        tmp = _ORIGIN + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh, indent=1, sort_keys=True)
        os.replace(tmp, _ORIGIN)
    except Exception:
        pass


def _drop_origin(s: str) -> None:
    d = _origins()
    if d.pop(s, None) is not None:
        try:
            tmp = _ORIGIN + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(d, fh, indent=1, sort_keys=True)
            os.replace(tmp, _ORIGIN)
        except Exception:
            pass


def hidden() -> set:
    """Shipped tiles suppressed on THIS install.

    Shipped artwork lives inside the add-on image — read-only, and recreated by
    every update, so it can't be deleted on the box. Without this a tech who
    disagreed with a shipped tile was stuck with it. Hiding one falls back to
    the wordmark; the record is local, so an update can't resurrect it."""
    try:
        with open(_HIDDEN) as fh:
            d = json.load(fh)
        return set(d) if isinstance(d, list) else set()
    except Exception:
        return set()


def _set_hidden(slugs) -> None:
    try:
        os.makedirs(os.path.dirname(_HIDDEN), exist_ok=True)
        tmp = _HIDDEN + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(sorted(slugs), fh, indent=1)
        os.replace(tmp, _HIDDEN)
    except Exception:
        pass


def packages() -> dict:
    """Android package id -> tile slug.

    The package id is the device's own immutable identity; the app NAME is just
    what an installer typed. Resolving artwork by id means a tile can't go
    blank because someone wrote "Kayo Sports" instead of "Kayo"."""
    try:
        with open(os.path.join(_BUNDLED, "packages.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return {k: v for k, v in d.items()
                if isinstance(v, str) and not k.startswith("_")}
    except Exception:
        return {}


def tile_path(name_or_slug: str, package: str = ""):
    """Tile for an app. Package id wins over the display name when we have it;
    otherwise fall back to the name, alias-aware. Uploaded (Tech Tools) beats
    shipped, so a tech can update artwork without waiting for a release."""
    s = ""
    if package:
        s = packages().get(package.strip(), "")
    if not s:
        s = slug(name_or_slug)
    if not s:
        return None
    s = _aliases().get(s, s)
    up = os.path.join(_UPLOADED, s + ".png")
    if os.path.isfile(up):
        return up
    if s in hidden():
        return None                       # suppressed here -> wordmark fallback
    p = os.path.join(_BUNDLED, s + ".png")
    return p if os.path.isfile(p) else None


def _ls(d) -> set:
    try:
        return {f[:-4] for f in os.listdir(d) if f.endswith(".png")}
    except Exception:
        return set()


def list_tiles() -> dict:
    """Everything the pack serves right now, with origin, for the manager UI.

    Artwork is no longer shipped inside the add-on image. Every tile is stored
    on the box and is either FETCHED (looked up by brand domain) or UPLOADED
    (supplied by a tech, which always wins). Two states, both deletable — no
    read-only artwork to work around, no override/hidden bookkeeping."""
    shipped, stored, hid = _ls(_BUNDLED), _ls(_UPLOADED), hidden()
    org = _origins()
    tiles = []
    for s in sorted(shipped | stored):
        if s in stored:
            origin = org.get(s, "uploaded")
        elif s in hid:
            origin = "hidden"
        else:
            origin = "shipped"
        tiles.append({"slug": s, "origin": origin, "removable": True})
    return {"tiles": tiles, "aliases": _aliases()}


def save_upload(name_or_slug: str, data_url_or_b64: str, origin: str = "uploaded") -> dict:
    """Store a tile PNG (data: URL or bare base64).

    `origin` records how it got here so the manager can say plainly whether a
    tile was found automatically or put there deliberately."""
    s = slug(name_or_slug)
    if not s:
        return {"error": "name required"}
    raw = (data_url_or_b64 or "").strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        data = base64.b64decode(raw)
    except Exception:
        return {"error": "couldn't read the image data"}
    if len(data) < 100:
        return {"error": "image looks empty"}
    if len(data) > 1_500_000:
        return {"error": "image too large — keep tiles under ~1.5 MB"}
    os.makedirs(_UPLOADED, exist_ok=True)
    with open(os.path.join(_UPLOADED, s + ".png"), "wb") as fh:
        fh.write(data)
    _set_origin(s, origin)
    # Putting artwork back un-hides it — nobody means "store this and keep it
    # hidden".
    h = hidden()
    if s in h:
        h.discard(s)
        _set_hidden(h)
    return {"ok": True, "slug": s, "origin": "fetched" if origin == "fetched" else "uploaded"}


def delete_tile(name_or_slug: str) -> dict:
    """Remove a tile, doing the right thing for where it came from.

    An upload is deleted outright — if a shipped tile sits underneath, that one
    comes back. A shipped tile can't be deleted (it's inside the read-only
    add-on image and returns with every update), so it gets suppressed on this
    install instead and falls back to the wordmark. Either way the tech gets
    what they asked for: the tile stops appearing."""
    s = slug(name_or_slug)
    if not s:
        return {"error": "which tile?"}
    up = os.path.join(_UPLOADED, s + ".png")
    ship = os.path.isfile(os.path.join(_BUNDLED, s + ".png"))
    if os.path.isfile(up):
        os.remove(up)
        _drop_origin(s)
        if ship:
            return {"ok": True, "slug": s, "action": "reverted",
                    "note": "removed — the tile that ships with ProOS is showing again"}
        return {"ok": True, "slug": s, "action": "deleted"}
    if ship:
        h = hidden(); h.add(s); _set_hidden(h)
        return {"ok": True, "slug": s, "action": "hidden",
                "note": "hidden on this system — it shows as a wordmark until you restore it"}
    return {"error": "no such tile"}


def restore_tile(name_or_slug: str) -> dict:
    """Un-hide a shipped tile that was suppressed here."""
    s = slug(name_or_slug)
    h = hidden()
    if s not in h:
        return {"error": "that tile isn't hidden"}
    h.discard(s)
    _set_hidden(h)
    return {"ok": True, "slug": s, "action": "restored"}


# Platform furniture, not content apps. A device lists these alongside real
# apps, but nobody puts a shortcut to Settings on a room page — and chasing
# brand artwork for them is wasted effort. Bucketed separately so the artwork
# checklist stays the list that actually matters.
_SYSTEM = {
    "settings", "search", "computers", "app_store", "play_store",
    "facetime", "photos", "podcasts", "fitness", "arcade", "music",
    "speedtest", "nordvpn", "tv_shows", "movies",
    # TV / AVR menu furniture that arrives in source_list beside real apps
    "gallery", "history", "favorites", "favourites", "playlists", "internet",
    "privacy_choices", "e_manual", "universal_guide", "source", "smartthings",
    "local_music", "music_assistant_queue", "heos_music", "quick_select",
}
# An AV input is not an app and will never have brand artwork. These arrive in
# the same source_list as real apps (AVRs and TVs publish inputs there), and
# without this the artwork checklist fills up with HDMI 1-4 and phono.
_INPUT_RE = re.compile(
    r"^(hdmi|aux|av|component|composite|optical|coax|digital|analog|analogue|"
    r"line|input|in|out|zone|usb|scart|vga|dvi|displayport|arc|earc|ext|"
    r"media_player|net|tv_audio|audio)(_?\d+)?$"
    r"|^(line|aux|audio|video)_(in|out)$"
    r"|^(tuner|phono|cd|dvd|blu_ray|bluray|tape|vcr|stb|sat|cable|game|pc|dock|"
    r"bluetooth|airplay|spdif|preout|multi_ch|pure_direct|radio|dab|fm|am|"
    r"flash|screen_mirroring|miracast|anynet|cec)$"
    # A connected box is a SOURCE, not an app: a TV listing "Shield" or
    # "Apple TV" as an input means the device on HDMI 2, not something to launch.
    r"|^(shield|nvidia_shield|firestick|fire_tv|chromecast|roku|set_top_box)$",
    re.I)


def is_app(name_or_slug: str) -> bool:
    """Is this a launchable app, or the device's own plumbing?"""
    s = _aliases().get(slug(name_or_slug), slug(name_or_slug))
    return bool(s) and s not in _SYSTEM and not _INPUT_RE.match(s)


def audit(app_names) -> dict:
    """Which of the apps this home ACTUALLY has are still missing a tile.

    `app_names` is every app name reported by every media player on site (Apple
    TV source_list, the Android ledger, Samsung inputs, anything added later).
    Turns "why is this one a wordmark" into a finite checklist of real files to
    supply, rather than a guess at what a home might own."""
    have, need, system = [], [], []
    seen = set()
    for nm in (app_names or []):
        s = slug(nm)
        if not s or s in seen:
            continue
        seen.add(s)
        canon = _aliases().get(s, s)
        rec = {"name": nm, "slug": s, "file": canon + ".png"}
        if tile_path(nm):
            have.append(rec)
        elif not is_app(nm):
            system.append(rec)
        else:
            need.append(rec)
    for lst in (have, need, system):
        lst.sort(key=lambda r: r["name"].lower())
    return {"have": have, "missing": need, "system": system,
            "counts": {"have": len(have), "missing": len(need),
                       "system": len(system),
                       "total": len(have) + len(need) + len(system)}}


# ── automatic sourcing ───────────────────────────────────────────────────────
# Logos can be looked up by domain instead of hunted down by hand. ProOS fetches
# ONCE, builds the tile, and stores it locally — it never links a dashboard to
# a third party. A control system has to keep working with the internet down,
# a wall tablet full of broken tiles is not acceptable, and an outage or a
# change of terms upstream would otherwise degrade every install at once.
_LOGO_CDN = "https://cdn.brandfetch.io"
_FETCH_TIMEOUT = 20


def domains() -> dict:
    """Curated slug -> brand domain map. Deliberately not guessed: a wrong
    domain fetches another company's logo, which is worse than no tile."""
    try:
        with open(os.path.join(_BUNDLED, "domains.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return {k: v for k, v in d.items()
                if isinstance(v, str) and not k.startswith("_")}
    except Exception:
        return {}


def brand_domain(slug_or_name: str) -> str:
    s = slug(slug_or_name)
    return domains().get(_aliases().get(s, s)) or domains().get(s) or ""


def source_url(slug_or_name: str, client_id: str, domain: str = "",
               kind: str = "logo", theme: str = "dark", fallback: str = "404") -> str:
    """Where a brand's artwork lives.

    `theme=dark` asks for the variant intended to sit ON a dark background —
    i.e. the light logo, which is what a tile needs.

    `fallback=404` is the important one: by default the CDN answers an unknown
    brand with a generic placeholder (their own mark or a lettermark), which
    sails through as a valid image and gets stored as if it were real artwork.
    Asking it to 404 instead means "no logo" fails honestly."""
    dom = (domain or "").strip() or brand_domain(slug_or_name)
    if not dom:
        return ""
    return "%s/domain/%s/fallback/%s/theme/%s/w/1200/h/1200/%s?c=%s" % (
        _LOGO_CDN, dom, fallback, theme, kind, client_id)


def fetch_source(slug_or_name: str, client_id: str, domain: str = "",
                 kind: str = "logo", theme: str = "dark") -> dict:
    """Download a brand's logo. Returns it as a data URL for the caller to
    composite — Core never processes images itself (no image libraries, by
    design: zero third-party Python dependencies)."""
    import urllib.request, urllib.error                          # noqa: E401
    s = slug(slug_or_name)
    if not client_id:
        return {"error": "no Brandfetch client ID set — add it in the add-on configuration"}
    if not brand_domain(s) and not (domain or "").strip():
        return {"error": "no domain known for '%s' — add it to appart/domains.json" % s}

    def _get(fb):
        url = source_url(s, client_id, domain, kind, theme, fallback=fb)
        req = urllib.request.Request(url, headers={"User-Agent": "ProOS-Core"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            ct = (resp.headers.get("Content-Type") or "image/png").split(";")[0].strip()
            return url, ct, resp.read()

    try:
        try:
            url, ctype, data = _get("404")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Honest miss: this brand genuinely has no published logo.
                return {"error": "no artwork published for that brand"}
            if e.code == 400:
                # Older/changed URL grammar won't accept the fallback segment.
                # Retry plainly rather than failing the whole run.
                url, ctype, data = _get("transparent")
            else:
                raise
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": "Brandfetch rejected the client ID (HTTP %d)" % e.code}
        if e.code == 429:
            return {"error": "Brandfetch rate limit reached — try again later"}
        return {"error": "lookup failed (HTTP %d)" % e.code}
    except Exception as e:                                       # noqa: BLE001
        return {"error": "couldn't reach Brandfetch (%s)" % e}
    if len(data) < 100:
        return {"error": "empty response"}
    if len(data) > 4_000_000:
        return {"error": "artwork too large"}
    return {"ok": True, "slug": s, "url": url, "content_type": ctype,
            "image": "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode())}


def import_zip(data_url_or_b64: str) -> dict:
    """Bulk-add tiles from a ZIP of images — the fast way to grow the pack.

    Every image in the archive becomes a tile named from its FILE NAME, so a
    folder of brand logos exported from anywhere drops straight in. Folders
    inside the zip are ignored; only the file name matters."""
    raw = (data_url_or_b64 or "").strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    try:
        blob = base64.b64decode(raw)
    except Exception:
        return {"error": "couldn't read the archive"}
    if len(blob) > 40_000_000:
        return {"error": "archive too large — keep it under ~40 MB"}
    added, skipped = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                base = os.path.basename(info.filename)
                stem, ext = os.path.splitext(base)
                if ext.lower() not in (".png", ".jpg", ".jpeg", ".webp") \
                        or base.startswith((".", "__")):
                    continue
                s = slug(stem)
                if not s:
                    continue
                if info.file_size > 1_500_000:
                    skipped.append({"file": base, "why": "over 1.5 MB"})
                    continue
                os.makedirs(_UPLOADED, exist_ok=True)
                with open(os.path.join(_UPLOADED, s + ".png"), "wb") as fh:
                    fh.write(z.read(info))
                added.append(s)
    except zipfile.BadZipFile:
        return {"error": "that isn't a ZIP archive"}
    if not added and not skipped:
        return {"error": "no images found in the archive"}
    return {"ok": True, "added": sorted(set(added)), "skipped": skipped}


def export_zip() -> dict:
    """Every tile uploaded on THIS install, as a ZIP.

    This is how a site-added tile gets promoted into the product: a tech
    uploads artwork to fix one home, exports it here, and those files go into
    the release's appart/ folder so every future install has it. Uploads are a
    staging area for the curated pack, not a private fork of it."""
    names = sorted(_ls(_UPLOADED))
    if not names:
        return {"error": "no uploaded tiles on this install"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for s in names:
            z.write(os.path.join(_UPLOADED, s + ".png"), s + ".png")
    return {"ok": True, "count": len(names), "tiles": names,
            "zip_b64": base64.b64encode(buf.getvalue()).decode()}


def clear_all(what: str = "all") -> dict:
    """Wipe stored artwork and start clean.

    'fetched' clears only what was looked up automatically, leaving a tech's own
    uploads alone — the usual case after fixing a domain or a bad source.
    'all' removes everything. Nothing here is destructive beyond this box: every
    fetched tile can be pulled again in one click."""
    org = _origins()
    gone = []
    for s in sorted(_ls(_UPLOADED)):
        if what == "fetched" and org.get(s, "uploaded") != "fetched":
            continue
        try:
            os.remove(os.path.join(_UPLOADED, s + ".png"))
            gone.append(s)
            _drop_origin(s)
        except Exception:
            pass
    if what != "fetched":
        _set_hidden(set())                    # nothing left to be hidden
    return {"ok": True, "removed": gone, "count": len(gone)}


def catalogue_gaps() -> list:
    """Every brand ProOS knows a domain for that has no tile yet.

    Pulling the whole catalogue rather than only this home's apps means the
    artwork is already there the day a customer installs an app for the first
    time — no wordmark, no call-out, nothing to chase."""
    return [{"slug": s, "domain": d, "file": s + ".png"}
            for s, d in sorted(domains().items()) if not tile_path(s)]


def status() -> dict:
    return list_tiles()
