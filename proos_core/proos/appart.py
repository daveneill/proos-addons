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


def tile_path(name_or_slug: str):
    """Tile for an app (alias-aware). Uploaded (Tech Tools) beats shipped, so a
    tech can update artwork on a live install without waiting for a release."""
    s = slug(name_or_slug)
    if not s:
        return None
    s = _aliases().get(s, s)
    up = os.path.join(_UPLOADED, s + ".png")
    if os.path.isfile(up):
        return up
    p = os.path.join(_BUNDLED, s + ".png")
    return p if os.path.isfile(p) else None


def _ls(d) -> set:
    try:
        return {f[:-4] for f in os.listdir(d) if f.endswith(".png")}
    except Exception:
        return set()


def list_tiles() -> dict:
    """Everything the pack serves right now, with origin, for the manager UI."""
    shipped, uploaded = _ls(_BUNDLED), _ls(_UPLOADED)
    tiles = [{"slug": s,
              "origin": "uploaded" if s in uploaded else "shipped",
              "overrides": (s in uploaded and s in shipped)}
             for s in sorted(shipped | uploaded)]
    return {"tiles": tiles, "aliases": _aliases()}


def save_upload(name_or_slug: str, data_url_or_b64: str) -> dict:
    """Store a tile PNG uploaded from Pro Tech Tools (data: URL or bare base64)."""
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
    return {"ok": True, "slug": s}


def delete_tile(name_or_slug: str) -> dict:
    """Remove an UPLOADED tile. Shipped tiles live in the release image and
    can't be deleted on-box — uploading the same slug overrides them instead."""
    s = slug(name_or_slug)
    p = os.path.join(_UPLOADED, s + ".png")
    if not os.path.isfile(p):
        if os.path.isfile(os.path.join(_BUNDLED, s + ".png")):
            return {"error": "that tile ships with ProOS — upload a replacement to override it"}
        return {"error": "no such tile"}
    os.remove(p)
    return {"ok": True, "slug": s}


# Platform furniture, not content apps. A device lists these alongside real
# apps, but nobody puts a shortcut to Settings on a room page — and chasing
# brand artwork for them is wasted effort. Bucketed separately so the artwork
# checklist stays the list that actually matters.
_SYSTEM = {
    "settings", "search", "computers", "app_store", "play_store",
    "facetime", "photos", "podcasts", "fitness", "arcade", "music",
    "speedtest", "nordvpn", "tv_shows", "movies",
}


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
        elif canon in _SYSTEM:
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


def source_url(slug_or_name: str, client_id: str, domain: str = "",
               kind: str = "logo", theme: str = "dark") -> str:
    """Where a brand's artwork lives. `theme=dark` means the variant intended
    to sit ON a dark background — i.e. the light logo, which is what a tile
    needs."""
    s = slug(slug_or_name)
    dom = (domain or "").strip() or domains().get(_aliases().get(s, s)) or domains().get(s)
    if not dom:
        return ""
    return "%s/domain/%s/theme/%s/%s?c=%s" % (_LOGO_CDN, dom, theme, kind, client_id)


def fetch_source(slug_or_name: str, client_id: str, domain: str = "",
                 kind: str = "logo", theme: str = "dark") -> dict:
    """Download a brand's logo. Returns it as a data URL for the caller to
    composite — Core never processes images itself (no image libraries, by
    design: zero third-party Python dependencies)."""
    import urllib.request, urllib.error                          # noqa: E401
    s = slug(slug_or_name)
    if not client_id:
        return {"error": "no Brandfetch client ID set — add it in the add-on configuration"}
    url = source_url(s, client_id, domain, kind, theme)
    if not url:
        return {"error": "no domain known for '%s' — add it to appart/domains.json" % s}
    req = urllib.request.Request(url, headers={"User-Agent": "ProOS-Core"})
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "image/png").split(";")[0].strip()
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": "Brandfetch rejected the client ID (HTTP %d)" % e.code}
        if e.code == 404:
            return {"error": "no artwork published for that brand"}
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


def status() -> dict:
    return list_tiles()
