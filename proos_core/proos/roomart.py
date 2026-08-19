"""
ProOS Core — room identity: generated background art + a matched icon, both
derived from the room's own NAME.

Why: a commissioned home should look bespoke before anyone uploads a single
photo. Every room gets a background generated from its name (same image engine
as scene photos) and an icon that matches what the room IS, so the same symbol
identifies that area everywhere — Pro, the dashboard, alerts.

Precedence, deliberately simple: an UPLOADED area picture always wins. Generated
art is written to the area's picture only when the area has none, so the
installer's or homeowner's own image is never overwritten.

Keyless homes still look finished: when no image key is configured, ROOM_STYLES
supplies a curated photograph matched on the same keywords as the icon.
"""
from __future__ import annotations
import re

# name keyword -> (mdi icon, curated fallback photo). First match wins, so the
# more specific rooms are listed before the generic ones.
ROOM_KINDS = [
    (("master bedroom", "master"), "mdi:bed-king",
     "https://images.unsplash.com/photo-1560185893-a55cbc8c57e8?w=1400&q=80"),
    (("bedroom", "bed room", "guest room"), "mdi:bed",
     "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?w=1400&q=80"),
    (("nursery", "baby"), "mdi:teddy-bear",
     "https://images.unsplash.com/photo-1586023492125-27b2c045efd7?w=1400&q=80"),
    (("living", "lounge", "sitting", "front room"), "mdi:sofa",
     "https://images.unsplash.com/photo-1600210492486-724fe5c67fb0?w=1400&q=80"),
    (("family", "rumpus", "den"), "mdi:sofa-outline",
     "https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?w=1400&q=80"),
    (("theatre", "theater", "cinema", "media room"), "mdi:theater",
     "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?w=1400&q=80"),
    (("kitchen", "pantry"), "mdi:countertop",
     "https://images.unsplash.com/photo-1556909212-d5b604d0c90d?w=1400&q=80"),
    (("dining", "dinner"), "mdi:table-furniture",
     "https://images.unsplash.com/photo-1617806118233-18e1de247200?w=1400&q=80"),
    (("office", "study", "desk", "work"), "mdi:desk",
     "https://images.unsplash.com/photo-1497366216548-37526070297c?w=1400&q=80"),
    (("bath", "ensuite", "shower", "powder"), "mdi:shower",
     "https://images.unsplash.com/photo-1584622650111-993a426fbf0a?w=1400&q=80"),
    (("laundry", "utility"), "mdi:washing-machine",
     "https://images.unsplash.com/photo-1626806787461-102c1bfaaea1?w=1400&q=80"),
    (("garage", "workshop", "shed"), "mdi:garage",
     "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=1400&q=80"),
    (("gym", "fitness"), "mdi:dumbbell",
     "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?w=1400&q=80"),
    (("pool", "spa"), "mdi:pool",
     "https://images.unsplash.com/photo-1576013551627-0cc20b96c2a7?w=1400&q=80"),
    (("outdoor", "patio", "alfresco", "deck", "garden", "yard", "balcony"), "mdi:tree",
     "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=1400&q=80"),
    (("hall", "entry", "foyer", "corridor", "stairs", "landing"), "mdi:door-open",
     "https://images.unsplash.com/photo-1600607687920-4e2a09cf159d?w=1400&q=80"),
    (("services", "plant", "comms", "rack"), "mdi:server-network",
     "https://images.unsplash.com/photo-1558494949-ef010cbdcc31?w=1400&q=80"),
    (("kids", "playroom", "play"), "mdi:toy-brick",
     "https://images.unsplash.com/photo-1558877385-81a1c7e67d72?w=1400&q=80"),
]
DEFAULT_ICON = "mdi:home-outline"
DEFAULT_PHOTO = "https://images.unsplash.com/photo-1600210492493-0946911123ea?w=1400&q=80"

# scene-style prompts read as moods; a room reads as a place. Kind-specific
# framing keeps a "Bedroom" from coming back as a hotel lobby.
_KIND_SCENE = {
    "mdi:bed-king": "a serene master bedroom", "mdi:bed": "a calm bedroom",
    "mdi:teddy-bear": "a soft, gentle nursery", "mdi:sofa": "an elegant living room",
    "mdi:sofa-outline": "a relaxed family room", "mdi:theater": "a private home cinema",
    "mdi:countertop": "a refined modern kitchen", "mdi:table-furniture": "an inviting dining room",
    "mdi:desk": "a focused home office", "mdi:shower": "a spa-like bathroom",
    "mdi:washing-machine": "a bright laundry room", "mdi:garage": "a clean, ordered garage",
    "mdi:dumbbell": "a home gym", "mdi:pool": "a tranquil pool at dusk",
    "mdi:tree": "a landscaped outdoor terrace", "mdi:door-open": "a welcoming entry hall",
    "mdi:server-network": "a tidy equipment rack room", "mdi:toy-brick": "a cheerful playroom",
}


# Extra curated photographs per kind, so two rooms of the same kind never
# share a background. Ordered; the identity pass hands out unused ones first.
KIND_VARIANTS = {
    "mdi:bed": [
        "https://images.unsplash.com/photo-1522771739844-6a9f6d5f14af?w=1400&q=80",
        "https://images.unsplash.com/photo-1616594039964-ae9021a400a0?w=1400&q=80",
        "https://images.unsplash.com/photo-1540518614846-7eded433c457?w=1400&q=80"],
    "mdi:bed-king": [
        "https://images.unsplash.com/photo-1618221195710-dd6b41faaea6?w=1400&q=80",
        "https://images.unsplash.com/photo-1611892440504-42a792e24d32?w=1400&q=80"],
    "mdi:sofa": [
        "https://images.unsplash.com/photo-1524758631624-e2822e304c36?w=1400&q=80",
        "https://images.unsplash.com/photo-1567767292278-a4f21aa2d36e?w=1400&q=80"],
    "mdi:sofa-outline": [
        "https://images.unsplash.com/photo-1618219908412-a29a1bb7b86e?w=1400&q=80",
        "https://images.unsplash.com/photo-1583847268964-b28dc8f51f92?w=1400&q=80"],
    "mdi:desk": [
        "https://images.unsplash.com/photo-1524758631624-e2822e304c36?w=1400&q=80",
        "https://images.unsplash.com/photo-1593642532973-d31b6557fa68?w=1400&q=80",
        "https://images.unsplash.com/photo-1585128792020-803d29415281?w=1400&q=80",
        "https://images.unsplash.com/photo-1544006659-f0b21884ce1d?w=1400&q=80"],
    "mdi:countertop": [
        "https://images.unsplash.com/photo-1600489000022-c2086d79f9d4?w=1400&q=80",
        "https://images.unsplash.com/photo-1600566753086-00f18fb6b3ea?w=1400&q=80"],
    "mdi:home-outline": [
        "https://images.unsplash.com/photo-1616486338812-3dadae4b4ace?w=1400&q=80",
        "https://images.unsplash.com/photo-1616137466211-f939a420be84?w=1400&q=80",
        "https://images.unsplash.com/photo-1567016432779-094069958ea5?w=1400&q=80",
        "https://images.unsplash.com/photo-1560448204-e02f11c3d0e2?w=1400&q=80",
        "https://images.unsplash.com/photo-1598928506311-c55ded91a20c?w=1400&q=80"],
}

# every icon Pro may offer in the picker, grouped for the sheet
ICON_CHOICES = [
    ("Sleeping", ["mdi:bed", "mdi:bed-king", "mdi:teddy-bear"]),
    ("Living", ["mdi:sofa", "mdi:sofa-outline", "mdi:theater", "mdi:toy-brick"]),
    ("Cooking & dining", ["mdi:countertop", "mdi:table-furniture"]),
    ("Working", ["mdi:desk", "mdi:server-network"]),
    ("Utility", ["mdi:shower", "mdi:washing-machine", "mdi:garage", "mdi:dumbbell"]),
    ("Outside", ["mdi:pool", "mdi:tree", "mdi:door-open"]),
    ("Other", ["mdi:home-outline"]),
]


def pictures_needed(areas, project) -> dict:
    """{area_id: curated_url} for every COMMITTED room whose HA area exists
    and has NO picture. Pure; benched (tests/roomart_pictures_bench.py).

    Found after the 31 Jul factory reset: a room saved without a background
    LOOKED fine only because each surface invented its own default — Pro
    previews this module's table, the dashboard falls back to its own older
    map, and nothing ever wrote the area picture, so Pro's generate-count
    kept saying "1 room without a background". One writer fixes all of it:
    lock the curated background into the area picture at commit, and every
    surface reads the same image from the same place.

    Rules: an existing picture is never overwritten (uploads always win);
    uncommitted rooms and missing areas are untouched; two rooms of one kind
    get different variants, handed out deterministically (sorted by area_id)
    and never repeating a URL an existing area picture already uses.
    """
    by_id = {a.get("area_id"): a for a in (areas or [])
             if isinstance(a, dict) and a.get("area_id")}
    used = {a.get("picture") for a in by_id.values() if a.get("picture")}
    out = {}
    recs = [(k, r) for k, r in ((project or {}).get("areas") or {}).items()
            if isinstance(r, dict) and r.get("committed")]
    for key, rec in sorted(recs, key=lambda kr: str(kr[1].get("area_id")
                                                    or kr[0])):
        aid = rec.get("area_id") or key
        area = by_id.get(aid)
        if area is None or area.get("picture"):
            continue
        pick = None
        for url in variants(rec.get("name") or aid):
            if url not in used:
                pick = url
                break
        if pick is None:                      # every variant in use: reuse 1st
            pick = variants(rec.get("name") or aid)[0]
        used.add(pick)
        out[aid] = pick
    return out


def variants(name: str) -> list:
    """Curated photographs for this room kind, best-first."""
    icon, photo = kind(name)
    out = [photo] + [v for v in KIND_VARIANTS.get(icon, []) if v != photo]
    # generic pool as a tail so a home with many same-kind rooms still differs
    for v in KIND_VARIANTS["mdi:home-outline"]:
        if v not in out:
            out.append(v)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


def kind(name: str):
    """(icon, curated_photo_url) for a room name — the single place the
    name→identity mapping lives, so Pro and the dashboard agree."""
    n = _norm(name)
    for keys, icon, photo in ROOM_KINDS:
        for k in keys:
            if k in n:
                return icon, photo
    return DEFAULT_ICON, DEFAULT_PHOTO


def icon_for(name: str) -> str:
    return kind(name)[0]


def photo_for(name: str) -> str:
    return kind(name)[1]


def build_prompt(name: str, mood: str | None = None) -> str:
    """Image prompt for a room background. The app applies its own dark scrim
    over the top, so the PHOTO must be well lit — an already-dark image goes
    black once the scrim lands on it. People and text are excluded because the
    picture sits behind live status chips."""
    icon, _ = kind(name)
    scene = _KIND_SCENE.get(icon, "an elegant interior space")
    extra = (" " + mood.strip()) if (mood or "").strip() else ""
    return ("A premium architectural interior photograph of %s in a modern "
            "luxury home.%s Bright natural daylight from large windows, soft "
            "even exposure, airy and clearly visible, warm neutral palette, "
            "wide establishing composition, photorealistic, professionally "
            "lit interior magazine photography, no people, no text, no logos."
            % (scene, extra))


def describe(name: str) -> dict:
    icon, photo = kind(name)
    return {"name": name, "icon": icon, "fallback_photo": photo,
            "prompt": build_prompt(name)}
