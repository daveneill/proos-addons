"""
ProOS Core -- per-scene photo store + image MATCHING (Pro Assist era).

The photo is a real property of the scene, not a side-effect of its name. The
assistant describes the scene's mood and Core MATCHES an actual photo to it via
image search (keyless Openverse API) — so "reading nook, warm lamp light" gets a
genuinely fitting image, not one of a handful of presets. The homeowner can
re-search, pick a different match, rename, or remove. Store keyed by scene
entity_id (identity; names are display only). Cleared by factory reset.
"""
from __future__ import annotations
import base64
import json
import os
import re
import shutil
import urllib.parse
import urllib.request

_STORE = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "scene_photos.json")
_SEARCH_URL = "https://api.openverse.org/v1/images/"
_OPENAI_IMG_URL = "https://api.openai.com/v1/images/generations"
_TIMEOUT = 8
_GEN_TIMEOUT = 90


def _www_dir() -> str:
    """HA's /www/proos_scenes — files there serve at /local/proos_scenes/… on
    every origin HA has (local and remote), so a generated scene image persists
    and loads in the dashboard without a temporary/expiring URL."""
    base = "/homeassistant" if os.path.isdir("/homeassistant") else "/config"
    d = os.path.join(base, "www", "proos_scenes")
    os.makedirs(d, exist_ok=True)
    return d


def build_prompt(name: str, mood: str | None) -> str:
    m = (mood or name or "").strip()
    return ("A premium, atmospheric interior photograph for a smart-home scene called '%s': %s. "
            "Cinematic natural lighting, elegant modern interior design, warm and inviting, "
            "photorealistic, no people, no text or logos, wide landscape composition." % (name, m))


def generate(prompt: str, api_key: str) -> bytes | None:
    """Generate a bespoke scene image via OpenAI (dall-e-3, landscape). Returns
    PNG bytes, or None on any failure (caller falls back to search/keyword)."""
    if not api_key:
        return None
    body = {"model": "dall-e-3", "prompt": prompt, "n": 1,
            "size": "1792x1024", "response_format": "b64_json"}
    req = urllib.request.Request(
        _OPENAI_IMG_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_GEN_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
        b64 = (d.get("data") or [{}])[0].get("b64_json")
        return base64.b64decode(b64) if b64 else None
    except Exception:
        return None


def save_generated(slug: str, png_bytes: bytes) -> str | None:
    """Write a generated image to /www and return its /local path, or None."""
    slug = re.sub(r"[^a-z0-9_]+", "_", (slug or "scene").lower()) or "scene"
    try:
        p = os.path.join(_www_dir(), slug + ".png")
        with open(p, "wb") as fh:
            fh.write(png_bytes)
        return "/local/proos_scenes/%s.png" % slug
    except Exception:
        return None

# Offline fallback only — used when image search is unreachable so a scene is
# never left without a picture. NOT a menu the user picks from.
_FALLBACK = [
    (("movie", "cinema", "film", "tv"), "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?w=1200&q=80"),
    (("dinner", "dining", "eat", "meal", "supper"), "https://images.unsplash.com/photo-1414235077428-338989a2e8c0?w=1200&q=80"),
    (("relax", "cozy", "cosy", "reading", "nook", "lounge", "evening"), "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=1200&q=80"),
    (("night", "sleep", "bed"), "https://images.unsplash.com/photo-1507400492013-162706c8c05e?w=1200&q=80"),
    (("morning", "sunrise", "wake", "dawn"), "https://images.unsplash.com/photo-1470252649378-9c29740c9fa8?w=1200&q=80"),
    (("party", "entertain", "gather", "games", "friends"), "https://images.unsplash.com/photo-1529543544282-ea669407fca3?w=1200&q=80"),
    (("work", "office", "focus", "study"), "https://images.unsplash.com/photo-1497366216548-37526070297c?w=1200&q=80"),
    (("away", "leave", "travel", "out"), "https://images.unsplash.com/photo-1436491865332-7a61a109cc05?w=1200&q=80"),
]
_DEFAULT = "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=1200&q=80"


def _fallback(text: str) -> str:
    low = (text or "").lower()
    for kws, url in _FALLBACK:
        for kw in kws:
            if re.search(r"\b%s" % re.escape(kw), low):
                return url
    return _DEFAULT


def search(query: str, n: int = 8) -> list:
    """Return up to n candidate image URLs matching the query (Openverse, keyless,
    landscape, commercial-use). Empty list on any failure — callers fall back."""
    q = (query or "").strip()
    if not q:
        return []
    params = urllib.parse.urlencode({
        "q": q, "page_size": max(1, min(int(n), 20)),
        "aspect_ratio": "wide", "license_type": "commercial",
        "mature": "false"})
    req = urllib.request.Request(
        _SEARCH_URL + "?" + params,
        headers={"User-Agent": "ProOS/1.0 (scene photos)"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = []
        for it in (data.get("results") or []):
            u = it.get("url")
            if u and (u.startswith("http://") or u.startswith("https://")):
                out.append(u)
        return out
    except Exception:
        return []


def match(query_or_name: str) -> str:
    """The single best-matched photo for a description or scene name: the top
    search result, or a keyword fallback if search is unavailable. A direct
    http(s) url passes straight through."""
    s = (query_or_name or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    hits = search(s, n=1)
    return hits[0] if hits else _fallback(s)


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
    """Store a scene's chosen photo URL and/or display-name override. `photo`
    here is an already-chosen URL (the tool/endpoint does the matching so it can
    also surface alternatives to a picker)."""
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
        shutil.rmtree(_www_dir(), ignore_errors=True)   # generated scene images
    except Exception:
        pass
