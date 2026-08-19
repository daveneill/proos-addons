"""
ProOS Core -- per-scene photo store, AI matching + upload (Pro Assist era).

A scene's photo is a real property of the scene, not a guess from its name.
Three ways it gets one, best-first:
  1. AI-GENERATED — the assistant (or the homeowner's "Generate" button)
     describes the mood and OpenAI makes a bespoke image (needs an image key).
  2. CURATED — a hand-picked premium image matched to the scene by keyword
     (the keyless fallback; always looks good).
  3. UPLOAD — the homeowner picks their own photo from the dashboard.
Generated/uploaded images are written to HA's /www so they serve at
/local/proos_scenes/… (persistent, local + remote). Store keyed by scene
entity_id. Cleared by factory reset.
"""
from __future__ import annotations
import base64
import json
import os
import re
import shutil
import time as _time
import urllib.error
import urllib.request

_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "scene_photos.json")
_OPENAI_IMG_URL = "https://api.openai.com/v1/images/generations"
_GEN_TIMEOUT = 90

# Image model, best-first (both use v1/images/generations and return b64_json).
# gpt-image-2 is OpenAI's current DEFAULT image model — state of the art, flexible
# sizes (confirmed on the model page, 6 Aug 2026). gpt-image-1 is kept as an
# interim fallback (it DEPRECATES 23 Oct 2026, so it buys time, not permanence).
# dall-e-3 was RETIRED 12 May 2026 and is removed — a dead model is not a fallback.
# Landscape (1536x1024) suits the scene/room cards; if a model is unavailable to
# the account (model-not-found) generate() advances to the next, otherwise it
# surfaces the real error and the caller uses a curated image.
_MODEL_ATTEMPTS = [("gpt-image-2", "1536x1024"), ("gpt-image-1", "1536x1024")]

# Curated premium catalog (keyless fallback + the "styles" picker). Every image
# is a polished, on-theme photo — so even without AI generation a scene looks
# good. label = what the picker shows; keywords drive auto-matching.
#
# THIS IS THE ONLY LIST. The dashboard used to carry a second copy of these
# URLs of its own; it now asks Core for this one (GET /scenes/photos), because
# two lists of the same thing is two chances to be wrong and Dave found both.
#
# A HOSTED URL IS NOT A READING — VERIFY BY EYE BEFORE YOU CHANGE ONE.
# 19 Aug 2026: Dave's homeowner Scenes page showed his "Work" scene as a man
# handling a rifle. Nothing had matched wrongly — the picture BEHIND a URL that
# had sat here for weeks had changed. Rendering all nine in a browser found
# three that no longer showed what their key says:
#   relax → a man with a rifle-like object   (and relax is the default, so this
#                                             was every unmatched scene's photo)
#   work  → a collage of three empty rooms
#   party → a close-up of scallops
# All three replaced 19 Aug 2026, each one looked at on screen at card size
# before it was written here. Do the same next time: no bench can see a photo.
CATALOG = [
    {"key": "relax", "label": "Relax / Cosy",
     "keywords": ["relax", "cozy", "cosy", "reading", "nook", "lounge", "evening", "chill", "unwind", "calm", "serene", "tranquil"],
     "url": "https://images.unsplash.com/photo-1727707185480-a50e6090b58c?w=1400&q=80"},
    {"key": "movie", "label": "Movie / Cinema",
     "keywords": ["movie", "cinema", "film", "tv", "theatre", "theater"],
     "url": "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?w=1400&q=80"},
    {"key": "dinner", "label": "Dinner / Dining",
     "keywords": ["dinner", "dining", "eat", "meal", "supper", "lunch"],
     "url": "https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=1400&q=80"},
    {"key": "night", "label": "Night / Sleep",
     "keywords": ["night", "sleep", "bed", "goodnight", "bedtime"],
     "url": "https://images.unsplash.com/photo-1507400492013-162706c8c05e?w=1400&q=80"},
    {"key": "morning", "label": "Morning / Wake",
     "keywords": ["morning", "sunrise", "wake", "dawn", "breakfast"],
     "url": "https://images.unsplash.com/photo-1470252649378-9c29740c9fa8?w=1400&q=80"},
    {"key": "party", "label": "Party / Entertain",
     "keywords": ["entertain", "party", "gather", "games", "friends", "celebrate", "guests"],
     "url": "https://images.unsplash.com/photo-1519671482749-fd09be7ccebf?w=1400&q=80"},
    {"key": "work", "label": "Work / Focus",
     "keywords": ["work", "office", "focus", "study", "desk"],
     "url": "https://images.unsplash.com/photo-1651739084015-85af0539f960?w=1400&q=80"},
    {"key": "away", "label": "Away / Out",
     "keywords": ["away", "leave", "travel", "out", "holiday", "goodbye"],
     "url": "https://images.unsplash.com/photo-1436491865332-7a61a109cc05?w=1400&q=80"},
    {"key": "bright", "label": "Bright / Everyday",
     "keywords": ["bright", "day", "clean", "everyday", "all on"],
     "url": "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=1400&q=80"},
]
# The catch-all, NAMED and looked up live. It used to be CATALOG[0] — a
# position, so reordering the list silently moved which photo every unmatched
# scene got, and nobody would have seen it move.
_DEFAULT_KEY = "relax"


def default_url() -> str:
    """The photo an unmatched scene gets — found by key, never by position."""
    return next((c["url"] for c in CATALOG if c["key"] == _DEFAULT_KEY), "")


def styles_for(name: str) -> list:
    """The style strip the scene art editor shows, THIS SCENE'S MATCH FIRST.

    The first thumbnail is the photo the scene is already wearing when nobody
    has overridden it, so "the standard one" is never something the person has
    to hunt for. Mirrors roomart.variants() for a room. No duplicates."""
    best = match(name or "scene")
    return [best] + [c["url"] for c in CATALOG if c["url"] != best]


def catalog() -> list:
    """Picker options: [{key, label, url, keywords}] + the catch-all.

    keywords ride along because the dashboard does the same keyword match on
    scenes it discovered itself, and it must do it against THIS list."""
    return [{"key": c["key"], "label": c["label"], "url": c["url"],
             "keywords": list(c["keywords"])} for c in CATALOG]


def match(query_or_name: str) -> str:
    """Best CURATED photo for a description/name (keyword match). A direct
    http(s) or /local url passes straight through. Never fails."""
    s = (query_or_name or "").strip()
    if s.startswith(("http://", "https://", "/local/")):
        return s
    low = s.lower()
    for c in CATALOG:
        if c["key"] == low:
            return c["url"]
    for c in CATALOG:
        for kw in c["keywords"]:
            if re.search(r"\b%s" % re.escape(kw), low):
                return c["url"]
    return default_url()


# ── AI generation + file storage ─────────────────────────────────────────────

def _www_dir() -> str:
    base = "/homeassistant" if os.path.isdir("/homeassistant") else "/config"
    d = os.path.join(base, "www", "proos_scenes")
    os.makedirs(d, exist_ok=True)
    return d


def build_prompt(name: str, mood: str | None) -> str:
    m = (mood or name or "").strip()
    return ("A premium, atmospheric interior photograph for a smart-home scene called '%s': %s. "
            "Cinematic natural lighting, elegant modern interior design, warm and inviting, "
            "photorealistic, no people, no text or logos, wide landscape composition." % (name, m))


def _one_image(model: str, prompt: str, size: str, api_key: str):
    """One OpenAI images call. Returns (png_bytes, None) or (None, error).
    NOTE: response_format must NOT be sent — the current images API rejects it
    with 400 'Unknown parameter: response_format' (observed live). gpt-image-1
    returns b64_json; dall-e-* return a temporary URL — handle both."""
    body = {"model": model, "prompt": prompt, "n": 1, "size": size}
    req = urllib.request.Request(
        _OPENAI_IMG_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key}, method="POST")
    with urllib.request.urlopen(req, timeout=_GEN_TIMEOUT) as r:
        d = json.loads(r.read().decode("utf-8"))
    item = (d.get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), None
    url = item.get("url")
    if url:
        with urllib.request.urlopen(url, timeout=40) as ir:
            return ir.read(), None
    return None, "no image in response"


def generate(prompt: str, api_key: str):
    """Generate a scene image via OpenAI, landscape. Returns (png_bytes, None)
    on success or (None, error_string) so the caller can log exactly why it fell
    back — no silent failures. Uses gpt-image-2 (OpenAI's current default image
    model), falling back to the ageing gpt-image-1 only if the account can't
    reach gpt-image-2. See _MODEL_ATTEMPTS for the model timeline."""
    if not api_key:
        return None, "no image key"
    attempts = _MODEL_ATTEMPTS
    last_err = "no image in response"
    for model, size in attempts:
        try:
            png, err = _one_image(model, prompt, size, api_key)
            if png:
                return png, None
            last_err = err or last_err
        except urllib.error.HTTPError as e:  # noqa: BLE001
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8")[:300].replace("\n", " ")
            except Exception:
                pass
            last_err = "openai %s: %s" % (e.code, body_txt)
            # Only try the next model if this one is unavailable to the account;
            # a real content/auth error will repeat, so surface it.
            if not ("does not exist" in body_txt or "model" in body_txt.lower() and e.code == 404):
                return None, last_err
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    return None, last_err


def _slug(s):
    return re.sub(r"[^a-z0-9_]+", "_", (s or "scene").lower()) or "scene"


def save_generated(slug: str, png_bytes: bytes):
    try:
        p = os.path.join(_www_dir(), _slug(slug) + ".png")
        with open(p, "wb") as fh:
            fh.write(png_bytes)
        # ?v busts the browser/PWA cache when a scene's image is regenerated.
        return "/local/proos_scenes/%s.png?v=%d" % (_slug(slug), int(_time.time()))
    except Exception:
        return None


def save_upload(slug: str, data_url_or_b64: str):
    """Store a homeowner-uploaded image (data: URL or bare base64). Returns the
    /local path or None. Kept small — the dashboard downscales before upload."""
    s = (data_url_or_b64 or "").strip()
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    ext = "png"
    try:
        raw = base64.b64decode(s)
    except Exception:
        return None
    try:
        p = os.path.join(_www_dir(), _slug(slug) + "_u." + ext)
        with open(p, "wb") as fh:
            fh.write(raw)
        return "/local/proos_scenes/%s_u.%s?v=%d" % (_slug(slug), ext, int(_time.time()))
    except Exception:
        return None


# ── store ─────────────────────────────────────────────────────────────────────

def load() -> dict:
    try:
        with open(_STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: dict) -> None:
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, _STORE)


def set_photo(entity_id: str, photo: str | None = None, name: str | None = None) -> dict:
    entity_id = (entity_id or "").strip()
    if not entity_id:
        return {"error": "entity_id required"}
    d = load()
    rec = d.get(entity_id) or {}
    if photo is not None:
        # "" means REMOVE THE OVERRIDE, not "store an empty picture". The
        # scene then shows the curated photo matched to its name again —
        # which is the standard, and what Remove should give you back
        # (Dave, 19 Aug: "scenes to get the curated pics as standard").
        if str(photo).strip():
            rec["photo"] = photo
        else:
            rec.pop("photo", None)
    if name is not None:
        nm = name.strip()
        if nm:
            rec["name"] = nm
        else:
            rec.pop("name", None)
    d[entity_id] = rec
    _write(d)
    return {"ok": True, "entity_id": entity_id, "record": rec}


def remove(entity_id: str) -> dict:
    d = load()
    if entity_id in d:
        del d[entity_id]
        _write(d)
    return {"ok": True, "removed": entity_id}


def clear() -> None:
    try:
        if os.path.exists(_STORE):
            os.remove(_STORE)
    except Exception:
        pass
    try:
        shutil.rmtree(_www_dir(), ignore_errors=True)
    except Exception:
        pass
