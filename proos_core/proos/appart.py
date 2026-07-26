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
import json
import os
import re

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
        return d if isinstance(d, dict) else {}
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


def status() -> dict:
    return list_tiles()
