#!/usr/bin/env python3
"""
ProOS app tile builder — press-kit files in, finished tile pack out.

    python3 make_tiles.py <logos-folder> [-o appart] [--bg tilebg.json] [--dry]

Point it at a folder of brand assets exactly as they come out of press kits.
It works out which file belongs to which app, picks the best variant when a
brand ships six of them, and writes one 600x360 PNG per app named by slug —
the same slug the dashboard asks Core for.

FORMATS       .svg .pdf .ai .eps  (vector, rendered at high resolution)
              .png .webp .jpg     (raster)

PICKING A VARIANT — press kits give you Netflix_Logo_RGB_White.svg,
netflix-wordmark-black.eps, netflix icon.png. The builder groups them by app,
then prefers:
    vector over raster        (sharp at any size)
    white / light / reverse   (most tiles sit on dark backgrounds)
    wordmark over icon-only   (reads better in a wide tile)
It prints what it chose for every app, so a wrong pick is obvious and you can
just delete the file you don't want and re-run. Use --dry to see the choices
without writing anything.

BACKGROUNDS   tilebg.json in the same folder: {"netflix": "#000000"}
              No entry -> derived from the logo. A colour that would make the
              supplied logo unreadable is ignored rather than shipped.

NEEDS         pip install pillow cairosvg      (vector PDF/AI/EPS also needs
                                                ImageMagick + Ghostscript)
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

W, H = 600, 360
MAX_LOGO_W, MAX_LOGO_H = 0.66, 0.52       # logo box inside the tile
DARK, LIGHT = (11, 11, 13), (245, 245, 247)
RENDER_W = 1600                            # vector render width before downscale

RASTER = (".png", ".webp", ".jpg", ".jpeg")
VECTOR = (".svg", ".pdf", ".ai", ".eps")

# Words that describe a VARIANT, not the app. Stripped when working out which
# app a file belongs to. "tv" and "play" deliberately stay — "Apple TV",
# "F1 TV" and "10 play" need them.
_NOISE = {
    "logo", "logos", "logotype", "wordmark", "word", "mark", "brand", "branding",
    "primary", "secondary", "alt", "alternate", "official", "asset", "assets",
    "rgb", "cmyk", "pms", "srgb", "hex",
    "white", "reverse", "reversed", "inverted", "inverse",
    "mono", "monochrome", "colour", "color", "full", "solid",
    "icon", "icons", "square", "symbol", "glyph", "badge", "favicon", "appicon",
    "horizontal", "vertical", "stacked", "lockup", "landscape", "portrait",
    "transparent", "bg", "background", "onlight", "ondark", "onblack", "onwhite",
    "final", "hires", "hi", "res", "large", "small", "med", "medium", "xl",
    "v1", "v2", "v3", "copy", "new", "master",
}
# Colour words are only variant names when the file is obviously a brand-kit
# export. Stripping them unconditionally would turn "Red Bull TV" into
# "bull_tv" and "BINGE black" into something else again — so they only count
# as noise alongside a marker like "logo" or "RGB".
_COLOURS = {"black", "dark", "light", "red", "green", "blue", "yellow", "orange",
            "purple", "pink", "grey", "gray", "teal", "navy", "gold", "silver"}
_KIT_MARKERS = {"logo", "logos", "logotype", "wordmark", "mark", "rgb", "cmyk",
                "pms", "brand", "branding", "asset", "assets", "primary",
                "secondary", "icon", "symbol"}
_LIGHT_HINT = ("white", "light", "reverse", "reversed", "inverted", "inverse", "ondark", "onblack")
_ICON_HINT = ("icon", "symbol", "glyph", "badge", "favicon", "appicon", "square")
# Brand kits ship usage guides beside the artwork — documents, not logos.
_DOCS = {"guide", "guides", "guideline", "guidelines", "spec", "specs", "readme",
         "manual", "usage", "rules", "dos", "donts", "instructions", "terms",
         "licence", "license", "toolkit", "overview"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def app_slug(stem: str) -> str:
    """Which APP a press-kit file belongs to, ignoring variant words.

    Colour words are only descriptors when they come AFTER the point where the
    file name stops naming the brand and starts describing the export. So
    "Spotify_Logo_RGB_Green" drops green, while "Red Bull TV logo white" keeps
    red — the brand always leads."""
    parts = [p for p in re.split(r"[^a-z0-9]+", stem.lower()) if p]
    first_marker = next((i for i, p in enumerate(parts) if p in _KIT_MARKERS), None)
    keep = []
    for i, p in enumerate(parts):
        if p in _NOISE or re.fullmatch(r"\d{3,4}", p):
            continue
        if p in _COLOURS and first_marker is not None and i > first_marker:
            continue
        keep.append(p)
    return "_".join(keep or parts)


# ── loading ──────────────────────────────────────────────────────────────────
def _render_svg(path: str) -> Image.Image:
    import cairosvg
    png = cairosvg.svg2png(url=path, output_width=RENDER_W)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _render_pdfish(path: str) -> Image.Image:
    """PDF / AI / EPS. Ghostscript first — many Linux builds of ImageMagick
    refuse PS/PDF outright via security policy, and gs keeps transparency."""
    ext = os.path.splitext(path)[1].lower()
    out = os.path.join(tempfile.mkdtemp(), "o.png")
    gs = shutil.which("gs")
    if gs:
        cmd = [gs, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pngalpha", "-r300"]
        if ext == ".eps":
            cmd.append("-dEPSCrop")
        cmd += ["-dLastPage=1", "-o", out, path]
        if subprocess.run(cmd, capture_output=True).returncode == 0 and os.path.exists(out):
            return Image.open(out).convert("RGBA")
    exe = shutil.which("magick") or shutil.which("convert")
    if not exe:
        raise RuntimeError("needs Ghostscript or ImageMagick to read %s" % ext)
    cmd = [exe] + (["convert"] if os.path.basename(exe) == "magick" else [])
    cmd += ["-density", "300", "-background", "none", path + "[0]",
            "-resize", "%dx" % RENDER_W, out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError((r.stderr.decode(errors="replace").strip() or "render failed")[:160])
    return Image.open(out).convert("RGBA")


def load(path: str) -> Image.Image:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".svg":
        return _render_svg(path)
    if ext in (".pdf", ".ai", ".eps"):
        return _render_pdfish(path)
    return Image.open(path).convert("RGBA")


# ── colour ───────────────────────────────────────────────────────────────────
def _lum(c) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def _contrast(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _ink_and_brand(img: Image.Image):
    small = img.resize((64, 64))
    px = small.load()
    tot, n = [0, 0, 0], 0
    best, best_sat = None, 0.0
    for y in range(64):
        for x in range(64):
            r, g, b, a = px[x, y]
            if a < 160:
                continue
            tot[0] += r; tot[1] += g; tot[2] += b; n += 1
            mx, mn = max(r, g, b), min(r, g, b)
            sat = 0.0 if mx == 0 else (mx - mn) / mx
            if sat > best_sat and mx > 60:
                best_sat, best = sat, (r, g, b)
    if not n:
        return (255, 255, 255), None
    return (tot[0] // n, tot[1] // n, tot[2] // n), (best if best_sat >= 0.45 else None)


def _background(img: Image.Image, override):
    ink, brand = _ink_and_brand(img)
    auto = (brand if brand and _contrast(ink, brand) >= 3.0
            else (DARK if _contrast(ink, DARK) >= _contrast(ink, LIGHT) else LIGHT))
    if override:
        s = str(override).lstrip("#")
        try:
            want = tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            return auto, None
        # A curated colour is chosen for the BRAND, but the file supplied might
        # be the light or the dark variant. If they'd cancel out, ignore it —
        # better a readable tile than a correct-but-invisible one.
        if _contrast(ink, want) >= 2.0:
            return want, None
        return auto, "background %s would hide this logo" % override
    return auto, None


def _has_transparency(img: Image.Image) -> bool:
    a = img.getchannel("A")
    if a.getextrema()[0] > 250:
        return False
    clear = sum(1 for v in a.resize((48, 48)).tobytes() if v < 40)
    return clear > (48 * 48) * 0.08


# ── build ────────────────────────────────────────────────────────────────────
def _logo_box(w: int, h: int) -> tuple[float, float]:
    """How much of the tile a logo may fill, by its shape.

    A wide wordmark and a tall symbol need different limits or they look
    wrong next to each other: constrain a wordmark by width, a symbol by
    height. Without this, the Netflix "N" (portrait) lands tiny in a wide tile
    beside a full-width wordmark."""
    ar = w / max(1, h)
    if ar >= 2.2:
        return 0.66, 0.42                     # wordmark / long lockup
    if ar >= 1.0:
        return 0.55, 0.55                     # squarish
    return 0.40, 0.66                         # portrait symbol


def build(img: Image.Image, bg_override=None):
    if not _has_transparency(img):
        sc = max(W / img.width, H / img.height)
        img = img.resize((max(1, round(img.width * sc)), max(1, round(img.height * sc))),
                         Image.LANCZOS)
        left, top = (img.width - W) // 2, (img.height - H) // 2
        return img.crop((left, top, left + W, top + H)).convert("RGB"), "cropped", None
    box = img.getbbox()                       # trim padding: uniform optical size
    if box:
        img = img.crop(box)
    bg, warn = _background(img, bg_override)
    max_w, max_h = _logo_box(img.width, img.height)
    sc = min((W * max_w) / img.width, (H * max_h) / img.height)
    logo = img.resize((max(1, round(img.width * sc)), max(1, round(img.height * sc))),
                      Image.LANCZOS)
    tile = Image.new("RGBA", (W, H), bg + (255,))
    tile.alpha_composite(logo, ((W - logo.width) // 2, (H - logo.height) // 2))
    return tile.convert("RGB"), "#%02x%02x%02x" % bg, warn


def _score(stem: str, ext: str) -> tuple:
    """Rank one candidate file for an app. Higher is better."""
    low = stem.lower()
    colour_space = 2 if "rgb" in low else (0 if ("cmyk" in low or "pms" in low) else 1)
    return (colour_space,                      # RGB is the screen space; CMYK/PMS are print
            2 if ext in VECTOR else 0,         # sharp at any size
            1 if any(h in low for h in _LIGHT_HINT) else 0,   # tiles are mostly dark
            -1 if any(h in low for h in _ICON_HINT) else 0,   # wordmark reads better
            -len(stem))                        # tie-break: the plainer name


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = argv[1]
    out = argv[argv.index("-o") + 1] if "-o" in argv else "appart"
    bgf = argv[argv.index("--bg") + 1] if "--bg" in argv else os.path.join(src, "tilebg.json")
    dry = "--dry" in argv
    try:
        with open(bgf) as fh:
            overrides = {slug(k): v for k, v in json.load(fh).items()
                         if isinstance(v, str) and not k.startswith("_")}
    except Exception:
        overrides = {}

    # Group every file by the app it belongs to, then keep the best variant.
    cands: dict[str, list] = {}
    for root, _dirs, files in os.walk(src):
        if "__MACOSX" in root.split(os.sep):
            continue
        for fn in files:
            stem, ext = os.path.splitext(fn)
            ext = ext.lower()
            if ext not in RASTER + VECTOR or fn.startswith((".", "__")):
                continue
            # Brand kits ship usage guides as PDFs alongside the artwork.
            # They're documents, not logos.
            if any(w in re.split(r"[^a-z0-9]+", stem.lower()) for w in _DOCS):
                continue
            cands.setdefault(app_slug(stem), []).append((os.path.join(root, fn), stem, ext))
    if not cands:
        print("No image or vector files found in %s" % src)
        return 1

    os.makedirs(out, exist_ok=True) if not dry else None
    made, failed = 0, []
    for s in sorted(cands):
        picks = sorted(cands[s], key=lambda c: _score(c[1], c[2]), reverse=True)
        others = len(picks) - 1
        # Work down the ranked variants: a kit's best file is sometimes one
        # this machine can't render, and the next one is usually just as good.
        img = path = None
        for cand_path, _stem, _ext in picks:
            try:
                img = load(cand_path)
                path = cand_path
                break
            except Exception as e:                               # noqa: BLE001
                last = "%s (%s)" % (os.path.basename(cand_path), e)
        if img is None:
            failed.append(s)
            print("  !! %-34s could not be read — %s" % (s, last))
            continue
        try:
            tile, how, warn = build(img, overrides.get(s))
        except Exception as e:                                   # noqa: BLE001
            failed.append(s)
            print("  !! %-34s %s" % (os.path.basename(path), e))
            continue
        if not dry:
            tile.save(os.path.join(out, s + ".png"), "PNG", optimize=True)
        made += 1
        print("  %-22s <- %-34s %-9s%s" % (
            s + ".png", os.path.basename(path), how,
            (" (+%d other variant%s)" % (others, "" if others == 1 else "s")) if others else ""))
        if warn:
            print("       note: %s — used a readable colour instead" % warn)

    print("\n%d tile%s %s%s" % (made, "" if made == 1 else "s",
                                "resolved (dry run)" if dry else "written to %s/" % out,
                                "  ·  %d failed" % len(failed) if failed else ""))
    if not dry:
        print("Drop them into the Core add-on's appart/ folder, or Import the "
              "folder as a ZIP in Pro → Tech Tools → App Tiles.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
