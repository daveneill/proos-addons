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

# Curated premium catalog (keyless fallback + the "styles" picker). Every image
# is a polished, on-theme photo — so even without AI generation a scene looks
# good. label = what the picker shows; keywords drive auto-matching.
CATALOG = [
    {"key": "relax", "label": "Relax / Cosy",
     "keywords": ["relax", "cozy", "cosy", "reading", "nook", "lounge", "evening", "chill", "unwind", "calm", "serene", "tranquil"],
     "url": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=1400&q=80"},
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
     "url": "https://images.unsplash.com/photo-1529543544282-ea669407fca3?w=1400&q=80"},
    {"key": "work", "label": "Work / Focus",
     "keywords": ["work", "office", "focus", "study", "desk"],
     "url": "https://images.unsplash.com/photo-1497366216548-37526070297c?w=1400&q=80"},
    {"key": "away", "label": "Away / Out",
     "keywords": ["away", "leave", "travel", "out", "holiday", "goodbye"],
     "url": "https://images.unsplash.com/photo-1436491865332-7a61a109cc05?w=1400&q=80"},
    {"key": "bright", "label": "Bright / Everyday",
     "keywords": ["bright", "day", "clean", "everyday", "all on"],
     "url": "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=1400&q=80"},
]
_DEFAULT = CATALOG[0]["url"]


def catalog() -> list:
    """Picker options: [{key, label, url}]."""
    return [{"key": c["key"], "label": c["label"], "url": c["url"]} for c in CATALOG]


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
    return _DEFAULT


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


def generate(prompt: str, api_key: str):
    """Generate a scene image via OpenAI (dall-e-3, landscape). Returns
    (png_bytes, None) on success or (None, error_string) so the caller can log
    exactly why it fell back — no silent failures. NOTE: response_format must
    NOT be sent — OpenAI's current images API rejects it with 400
    'Unknown parameter: response_format' (observed live). dall-e-3 then returns
    a temporary URL, which we fetch; gpt-image-1 returns b64_json — handle both."""
    if not api_key:
        return None, "no image key"
    body = {"model": "dall-e-3", "prompt": prompt, "n": 1, "size": "1792x1024"}
    req = urllib.request.Request(
        _OPENAI_IMG_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key}, method="POST")
    try:
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
    except urllib.error.HTTPError as e:  # noqa: BLE001
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8")[:300].replace("\n", " ")
        except Exception:
            pass
        return None, "openai %s: %s" % (e.code, body_txt)
    except Exception as e:  # noqa: BLE001
        return None, str(e)


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
        rec["photo"] = photo
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
