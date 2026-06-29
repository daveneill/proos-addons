# ProOS Music — Documentation

ProOS Music is ProOS's packaged build of **Music Assistant (MA)**. This page
explains, in plain terms, **what it does to a Home Assistant install when you add
it**, so it is added on purpose and never by reflex.

---

## 1. Where it sits in the stack

Speakers (Sonos, HEOS, AirPlay/HomePod, Chromecast, DLNA) are reached in Home
Assistant by their **own integrations**. Those integrations give you, per speaker:

- play / pause / volume / track info,
- **within-brand grouping** (Sonos⇄Sonos, HEOS⇄HEOS) via `media_player.join` /
  `media_player.unjoin`,
- **favourites / presets** browsing (Sonos favourites, HEOS presets), when a HEOS
  account is signed in or Sonos favourites exist on the system.

ProOS Music does **not replace** those integrations. It is a **separate layer on
top** that treats each speaker as an **output**. Think of it as a matrix/zone
controller, not a speaker driver.

```
        ┌─────────────────────────────────────────┐
        │            ProOS Music (MA)             │  ← optional layer
        │   cross-brand grouping + provider library │
        └───────────────┬─────────────────────────┘
                        │ plays to (output only)
   ┌──────────┬─────────┴───────┬────────────┬──────────┐
   │  Sonos   │      HEOS        │  AirPlay   │  DLNA …  │  ← native integrations
   │ (native) │    (native)      │ (HomePod)  │          │     own the speakers,
   └──────────┴─────────────────┴────────────┴──────────┘     favourites & state
```

## 2. The one thing it adds that nothing else can

**Grouping across brands.** Native grouping is always **within a single system** —
you cannot natively put a Sonos and a HomePod and a HEOS amp into one synchronised
group. ProOS Music can. If a site needs that, this add-on is the only way to get
it. If a site does not need that, this add-on adds nothing you don't already have.

## 3. The two things it does NOT give you

1. **It is not a source of your speakers' favourites.** Sonos favourites and HEOS
   presets are stored in those systems and exposed by the **native** HA
   integrations. Music Assistant treats Sonos/HEOS purely as outputs and never
   imports their favourites. **If you remove the native integrations and rely on
   this add-on alone, those favourites disappear from the system.**

2. **It is not music by itself.** A fresh install has an **empty library**. The
   smart playlists you see ("All favorited tracks", "Recently added", etc.) are
   templates that pull from a library — with no library they resolve to nothing
   and refuse to play ("No playable items found"). You must add a **Music
   provider** (Spotify / Apple Music / Tidal / local files / etc.) for it to have
   anything of its own to play.

## 4. The behaviour that surprises installers

Music Assistant maintains **its own playback queue** for each player and behaves
as though it is the controller. That is fine when **all** playback goes through
ProOS. But the moment a client uses the **native Sonos or HEOS app** to start or
regroup playback:

- the speaker does what the native app told it,
- MA's queue for that player goes **stale / empty**,
- you get log lines like `Resume queue requested but queue … is empty`, and
- control from ProOS can become inconsistent until playback is restarted through MA.

The native integrations do not behave this way. They hold no queue of their own —
they mirror the speaker system, which remains the single source of truth — so the
native app and ProOS always agree on what is playing and what is grouped.

**Implication:** on any site where clients will keep using the Sonos/HEOS app,
putting MA in the control path works against you.

## 5. What installing it actually changes on the box

- Registers a **second player entity** ("MA" twin) for every speaker MA can see.
  The ProOS homeowner dashboard hides these behind its speaker **allowlist**, so
  homeowners still see one clean player per room — but the twins exist in HA and
  will appear in HA's own UI and entity lists.
- Becomes the **default controller** for any playback ProOS starts.
- Runs as a host-network add-on (its API is on port `8095`; ingress on `8094`).
- Pulls in player providers automatically and a set of metadata providers.

## 6. Recommended ProOS policy

- **Default / standard:** native Sonos + HEOS (+ AirPlay for HomePods). These give
  per-room control, within-brand grouping, favourites, and clean native-app
  behaviour. **Do not install ProOS Music.**
- **Install ProOS Music only when** a site explicitly needs cross-brand synced
  grouping, or wants one streaming account playable on every brand — and even
  then, **keep the native integrations installed alongside it** so favourites and
  native-app behaviour are preserved.
- After installing, add at least one **Music provider** inside the add-on, or its
  library stays empty.

## 7. Quick reference

| Need | Native Sonos/HEOS | ProOS Music |
|---|---|---|
| Per-room play / volume | ✅ | ✅ |
| Within-brand grouping (Sonos⇄Sonos) | ✅ | ✅ |
| **Cross-brand grouping** (Sonos + HomePod) | ❌ | ✅ |
| Sonos favourites / HEOS presets | ✅ | ❌ |
| Clean coexistence with native Sonos/HEOS app | ✅ | ⚠️ desyncs |
| Streaming account playable to any brand | ❌ | ✅ (with a provider) |
| Works with an empty config | ✅ | ❌ (needs a provider) |
