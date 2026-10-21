"""MUSIC BY NAME ON ANY SPEAKER BRAND — the platform's own search (stage 6 of the planned build, 16 Oct 2026, register 896).

Josh plays "Adele in the study" whatever the speaker is. ProOS had its own roads for two brands (Sonos and HEOS, through
their own services) and Music Assistant where the installer added it; every other speaker answered "no music speaker in
that room".

THE PLATFORM ALREADY DOES THIS (the law: use the platform): `media_player.search_media` — a speaker's integration that
can search its own music answers with titles it can play (WiiM/LinkPlay, Squeezebox, Music Assistant, Sonos and more do;
an integration that can't answers "does not support action media_player.search_media"). Then `media_player.play_media`
with what it returned. What ProOS adds is only what only ProOS knows: WHICH speaker is the room's (the commissioned
room's audio), the SAME choosing rules every other road uses (musicsearch.rank — an exact artist first, several exact
kinds asked about, never the top result), and the check that it actually played.

search(client, eid, words) → (result, None) in musicsearch's shape — ONE "service" named for the speaker — or
                             (None, "unsupported") / (None, "<why it failed>").
play(client, eid, card)    → the platform's play_media with the card's own id and type. Raises on refusal.
"""
from proos import musicsearch as _ms

UNSUPPORTED = "unsupported"
_CLASS = {"artist": "artist", "track": "track", "song": "track", "album": "album", "playlist": "playlist",
          "channel": "station", "radio": "station", "podcast": "podcast", "episode": "podcast", "genre": "genre"}


def _kind(item):
    return _CLASS.get(str(item.get("media_class") or "").lower()) or _ms.kind_of(item.get("media_class"))


def search(client, eid, words, label=None, limit=20):
    """The speaker's own search, through the platform. Only titles the speaker says it can play are kept."""
    try:
        r = client._req("POST", "/api/services/media_player/search_media?return_response",
                        {"entity_id": eid, "search_query": words, "limit": limit}) or {}
    except Exception as e:                                       # noqa: BLE001
        msg = str(e)
        if "does not support" in msg or "not support action" in msg:
            return None, UNSUPPORTED
        return None, msg[:160] or "no answer"
    per = ((r.get("service_response") or {}).get(eid) or {}) if isinstance(r, dict) else {}
    items = per.get("result") if isinstance(per, dict) else None
    kinds = {}
    for it in items or []:
        if not isinstance(it, dict) or it.get("can_play") is False or not it.get("media_content_id"):
            continue
        k = _kind(it)
        kinds.setdefault(k, []).append({"id": it.get("media_content_id"), "type": it.get("media_content_type") or "music",
                                        "title": it.get("title") or "", "kind": k, "service": label or "",
                                        "art": it.get("thumbnail")})
    return {"q": words, "services": [{"name": label or eid, "kinds": kinds}]}, None


def play(client, eid, card):
    client._req("POST", "/api/services/media_player/play_media",
                {"entity_id": eid, "media_content_id": card["id"], "media_content_type": card.get("type") or "music"})
    return True
