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


def build_prompt(name: str) -> str:
    """Image prompt for a room background. Deliberately empty of people and
    text: it sits behind status chips and must never fight them."""
    icon, _ = kind(name)
    scene = _KIND_SCENE.get(icon, "an elegant interior space")
    return ("A premium architectural interior photograph of %s in a modern "
            "luxury home, for use as a dark app background. Moody low-key "
            "cinematic lighting, deep shadows, muted natural palette, shot "
            "wide, photorealistic, no people, no text, no logos, nothing in "
            "sharp focus in the centre of frame." % scene)


def describe(name: str) -> dict:
    icon, photo = kind(name)
    return {"name": name, "icon": icon, "fallback_photo": photo,
            "prompt": build_prompt(name)}
