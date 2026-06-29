# ProOS Music (optional)

> **Read this before installing.** ProOS Music is **not** required for music, and
> on most sites you should **not** install it. It exists for one specific job:
> **grouping speakers of different brands together** (for example a Sonos and a
> HomePod playing in sync). If a site only needs Sonos and/or HEOS multi-room,
> the native integrations already do everything — leave this add-on uninstalled.

ProOS Music is ProOS's packaged build of **Music Assistant** — a playback and
grouping engine that sits **on top of** your speakers and drives them as outputs.

## What it does

- Plays to, and controls the volume of, Sonos / HEOS / AirPlay / Chromecast / DLNA speakers.
- **Groups speakers across different brands** into one synced group. This is the
  only thing it does that the native integrations cannot.
- Optionally connects streaming **music providers** (Spotify, Apple Music, Tidal,
  local library, etc.) so the same source can play to any speaker.

## What it does NOT do

- It does **not** import or show your **Sonos favourites** or **HEOS presets**.
  Those live in the speaker systems and are surfaced by the **native HA Sonos /
  HEOS integrations** — not by this add-on. Removing the native integrations in
  favour of this add-on **removes those favourites from the system**.
- It is **not a music source by itself.** Out of the box its library is empty;
  until you add a streaming provider it has nothing of its own to play.

## The catch you must understand before adding it

Music Assistant keeps **its own playback queue** and assumes **it** is the thing
driving the speaker. When a client picks up the **native Sonos or HEOS app** and
starts or regroups playback there, ProOS Music's queue goes stale — you will see
"queue is empty / resume requested" behaviour and inconsistent control. The
native HA integrations do not have this problem: they simply mirror the speaker
system, so the native app and ProOS stay in agreement.

## Decision guide

| Site profile | Recommendation |
|---|---|
| Sonos only, or HEOS only | **Native integrations.** Do not install ProOS Music. |
| Clients will use the native Sonos / HEOS app | **Native integrations.** Do not install ProOS Music. |
| Mixed brands that must play **in one synced group** | Install ProOS Music **in addition** to the native integrations. |
| You want one streaming account playable on every brand | Install ProOS Music and add the provider inside it. |

## If you do install it

- Keep the **native Sonos / HEOS integrations installed as well** — they provide
  the favourites and the clean native-app behaviour; ProOS Music adds cross-brand
  grouping on top.
- ProOS Music registers a second ("MA") player entity for every speaker. The ProOS
  homeowner dashboard hides these via its speaker allowlist, but they exist in HA.
- Add at least one **Music provider** inside the add-on, or its library stays empty.

See the **Documentation** tab for the full explanation.
