"""ONE SEARCH, EVERY SERVICE — like the Sonos app (register 863; Dave 14 Oct 10:49: "the search needs to work like the
Sonos app — it searches all the added services and shows what it finds under each service, not search the selected
service").

Before this, ProOS searched ONE service — the one the speaker last played — so "Netflix" on myTuner Radio found
nothing carrying the name and the top station played (CSBC Life, seen on the box 10:35).

Now `search_all(client, entity_id, words)` asks EVERY service the speaker has, at the same time, each in its own
categories, and answers in one shape:

    {"q": words, "services": [{"service_id", "name", "account", "kinds": {"artist": [card…], "track": [...],
                                "album": [...], "playlist": [...], "station": [...], "other": [...]},
                               "ms": took, "pending": True when it hadn't answered in the budget, "error": why}],
     "order": [service names in the home's preferred order], "ms": total}

Cards are the service module's own (id, title, artist, art, type, open, play) plus `sid`, `acct`, `service`, `kind`
so a page or voice can play one without knowing which service it came from.

The Music page shows this grouped under each service; voice chooses from the same results (assist.py). One engine,
two faces. Results are kept KEPT_S per (speaker, words) — the page after the voice, or the same words again, cost
nothing. A service that is slow keeps answering into the kept result after the budget.

SERVICE ORDER (the home's preference; Settings › Music later writes `music_service_order` in the add-on options):
streaming services first, radio last — unless the words say radio or station. A name in `music_service_order`
goes first in that order. Nothing here names a particular service.
"""
import json
import os
import threading
import time

BUDGET_S = 2.5          # plumbing: how long one search waits for every service before answering with what it has
KEPT_S = 600            # plumbing: how long a search's answer is kept per (speaker, words)
SERVICES_KEPT_S = 600   # plumbing: how long the speaker's service list is kept
COUNT = 6               # how many items per category are asked of each service
OPTIONS_PATH = "/data/options.json"
RADIO_WORDS = ("radio", "tunein", "mytuner", "iheart", "station", "stations", "podcast")

_LOCK = threading.Lock()
_kept = {}              # (entity_id, words) -> (result, when)
_services = {}          # entity_id -> ((mod, sp, tz, [(sid, acct, name)]), when)


def kind_of(category):
    """A service's own category name as one of ours: artist, track, album, playlist, station, other."""
    c = str(category or "").lower()
    for k, words in (("artist", ("artist",)), ("track", ("track", "song")), ("album", ("album",)),
                     ("playlist", ("playlist",)), ("station", ("station", "radio", "stream")),
                     ("podcast", ("podcast", "show", "episode")), ("genre", ("genre", "mood"))):
        if any(w in c for w in words):
            return k
    return "other"


def is_radio(name):
    n = str(name or "").lower()
    return any(w in n for w in RADIO_WORDS)


def preferred_order(option=None):
    """The names the home put first (the add-on option), in order; [] when none."""
    if option is None:
        try:
            with open(OPTIONS_PATH, encoding="utf-8") as f:
                option = (json.load(f) or {}).get("music_service_order")
        except Exception:                                        # noqa: BLE001
            option = None
    if isinstance(option, str):
        option = [x.strip() for x in option.split(",")]
    return [str(x).strip().lower() for x in (option or []) if str(x).strip()]


def order_services(svcs, words="", option=None):
    """[(sid, acct, name)] in the home's preferred order: named first, then streaming, then radio —
    radio first when the words ask for radio or a station."""
    pref = preferred_order(option)
    want_radio = is_radio(words)

    def key(x):
        n = str(x[2] or "").lower()
        named = pref.index(n) if n in pref else len(pref)
        radio = is_radio(n)
        return (named, 0 if (radio == want_radio) else 1)
    return sorted(list(svcs), key=key)


def speaker_services(client, entity_id):
    """(mod, sp, tz, [(sid, acct, name)]) for the speaker, kept a while; None when the speaker has no services."""
    now = time.time()
    with _LOCK:
        hit = _services.get(entity_id)
    if hit and now - hit[1] < SERVICES_KEPT_S:
        return hit[0]
    from proos import speakermusic as _spm
    mod, sp = _spm.for_speaker(client, _spm.network_info(), entity_id)
    try:
        attrs = (client._req("GET", "/api/states/%s" % entity_id) or {}).get("attributes") or {}
    except Exception:                                            # noqa: BLE001
        attrs = {}
    svcs = mod.assist_services(sp, attrs)
    if not svcs:
        return None
    tz = _spm.home_time_zone(client)
    out = (mod, sp, tz, [(int(s), a, str(n)) for s, a, n in svcs])
    with _LOCK:
        _services[entity_id] = (out, now)
    return out


def forget_services(entity_id=None):
    with _LOCK:
        if entity_id is None:
            _services.clear()
        else:
            _services.pop(entity_id, None)


def _group(results, sid, acct, service):
    kinds = {}
    for grp in results or []:
        k = kind_of(grp.get("category"))
        for it in grp.get("items") or []:
            if not it.get("id"):
                continue
            c = dict(it, sid=sid, acct=acct, service=service, kind=k, category=grp.get("category"))
            kinds.setdefault(k, []).append(c)
    return kinds


def search_all(client, entity_id, words, budget_s=None, count=COUNT, searcher=None, services=None, now=time.time):
    """Every service of the speaker, at once. `searcher(mod, sp, sid, words, acct, tz, count)` and `services` stand in
    for the benches; on the box they are the service module's own."""
    words = " ".join(str(words or "").split())
    budget_s = BUDGET_S if budget_s is None else budget_s
    key = (entity_id, words.lower())
    t = now()
    with _LOCK:
        hit = _kept.get(key)
    if hit and t - hit[1] < KEPT_S:
        res = hit[0]
        return dict(res, kept=True, pending=[s["name"] for s in res["services"] if s.get("pending")])
    had = services if services is not None else speaker_services(client, entity_id)
    if not had:
        return {"q": words, "services": [], "order": [], "ms": 0, "error": "this speaker has no music services"}
    mod, sp, tz, svcs = had
    svcs = order_services(svcs, words)
    out = {"q": words, "services": [{"service_id": s, "account": a, "name": n, "kinds": {}, "pending": True, "ms": None}
                                    for s, a, n in svcs],
           "order": [n for _s, _a, n in svcs], "ms": 0}
    with _LOCK:
        _kept[key] = (out, t)
        if len(_kept) > 300:
            oldest = sorted(_kept.items(), key=lambda kv: kv[1][1])[:100]
            for k, _v in oldest:
                _kept.pop(k, None)

    def one(entry, sid, acct, name):
        t1 = now()
        try:
            if searcher is not None:
                res = searcher(mod, sp, sid, words, acct, tz, count)
            else:
                res = mod.search(sp, sid, words, acct, tz, count=count).get("results")
            kinds = _group(res, sid, acct, name)
            with _LOCK:
                entry["kinds"] = kinds
        except Exception as e:                                   # noqa: BLE001
            with _LOCK:
                entry["error"] = str(e)[:160]
        finally:
            with _LOCK:
                entry["pending"] = False
                entry["ms"] = int((now() - t1) * 1000)

    ts = [threading.Thread(target=one, args=(e, e["service_id"], e["account"], e["name"]), daemon=True,
                           name="music-search-%s" % e["name"]) for e in out["services"]]
    for th in ts:
        th.start()
    deadline = t + budget_s
    for th in ts:
        left = deadline - now()
        if left > 0:
            th.join(left)
    out["ms"] = int((now() - t) * 1000)
    with _LOCK:
        snap = dict(out)
        snap["services"] = [dict(s) for s in out["services"]]
    snap["pending"] = [s["name"] for s in snap["services"] if s.get("pending")]
    return snap


def forget(entity_id=None):
    with _LOCK:
        if entity_id is None:
            _kept.clear()
        else:
            for k in [k for k in _kept if k[0] == entity_id]:
                _kept.pop(k, None)


# ── What voice picks from the results (the design's rank; assist.py calls this) ───────────────────────────────
KIND_ORDER = ("artist", "track", "album", "playlist", "station", "podcast", "genre", "other")
KIND_SAID = {"artist": ("artist", "band"), "track": ("song", "track"), "album": ("album", "record"),
             "playlist": ("playlist", "mix"), "station": ("station", "radio", "stream"), "podcast": ("podcast", "episode")}


def _letters(s):
    return "".join(ch for ch in str(s or "").lower().replace("’", "'") if ch.isalnum() or ch == " ").split()


def same_words(a, b):
    return _letters(a) == _letters(b) and bool(_letters(a))


def carries(title, words):
    """The title carries the words: equal, or starts with them, or holds them whole in order."""
    t, w = _letters(title), _letters(words)
    if not w:
        return False
    if t == w or t[:len(w)] == w:
        return True
    return any(t[i:i + len(w)] == w for i in range(len(t) - len(w) + 1))


def kind_said(words):
    """(the kind the words name, the words without it) — "the album rumours" → ("album", "rumours"); (None, words) when none."""
    ws = _letters(words)
    for kind, said in KIND_SAID.items():
        for s in said:
            if ws and ws[0] == s and len(ws) > 1:
                return kind, " ".join(ws[1:])
            if len(ws) > 1 and ws[-1] == s:
                return kind, " ".join(ws[:-1])
            if len(ws) > 2 and ws[0] == "the" and ws[1] == s:
                return kind, " ".join(ws[2:])
    return None, " ".join(str(words or "").split())


def rank(result, words):
    """From a search_all result: ("exact", [options]) — exact-title matches of every kind in the FIRST service that has
    one (the design: several kinds there → ask); ("partial", [one option]) — the first result carrying the words, by
    service order then kind order; ("none", []) when nothing carries them. An option is the card itself."""
    kind, name = kind_said(words)
    name = name or words
    services = result.get("services") or []
    kinds_wanted = [kind] if kind else list(KIND_ORDER)
    for s in services:
        exact = []
        for k in kinds_wanted:
            for c in (s.get("kinds") or {}).get(k) or []:
                if same_words(c.get("title"), name):
                    exact.append(c)
                    break
        if exact:
            # register 864 (seen on the box 14 Oct 11:40: "the artist Adele on Spotify, the other Adele on Spotify"):
            # a name that IS an artist is the artist — "play Adele" is Adele; nothing to ask. And a service's shelf
            # of unknown kind ("other") never stands beside a known kind as if it were one.
            known = [c for c in exact if c.get("kind") in ("artist", "track", "album", "playlist", "station")]
            if known:
                exact = known
            artist = next((c for c in exact if c.get("kind") == "artist"), None)
            if artist is not None and not kind:
                return "exact", [artist]
            return "exact", exact
    for s in services:
        for k in kinds_wanted:
            for c in (s.get("kinds") or {}).get(k) or []:
                if carries(c.get("title"), name):
                    return "partial", [c]
    return "none", []


def nothing_found(result, words):
    names = [s["name"] for s in result.get("services") or [] if not s.get("pending")]
    slow = [s["name"] for s in result.get("services") or [] if s.get("pending")]
    msg = "nothing found for %s" % words
    if names:
        msg += " on " + (", ".join(names[:-1]) + " or " + names[-1] if len(names) > 1 else names[0])
    if slow:
        msg += " (%s %s still answering)" % (", ".join(slow), "was" if len(slow) == 1 else "were")
    return msg
