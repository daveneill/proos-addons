# ProOS Certified Apple TV Integration

A ProOS-certified fork of the Home Assistant core `apple_tv` integration.

## Why this exists
tvOS 26.5 changed the Companion protocol so that `FetchAttentionState` is no
longer handled (the device replies "No request handler"). On `pyatv==0.17.0`
(pinned by HA core 2026.6.x) this aborts power-state setup, so the integration
reports `Could not fetch SystemStatus, power_state will not work` and the Apple
TV entities flap / never settle their on-off state.

`pyatv==0.18.0` ("Willie", 2026-06-19) fixes this: it swallows the failed
initial fetch and subscribes to pushed `SystemStatus`/`TVSystemStatus` events
regardless, so power state recovers via push. HA core 2026.6.3 has NOT yet
bumped to 0.18.0.

## What this build changes (vs HA core 2026.6.3 `apple_tv`)
- `manifest.json`: `requirements` `pyatv==0.17.0` -> `pyatv==0.18.0`; added
  `version` (custom components require it). This is the tvOS 26.5 fix.
- `binary_sensor.py` (+ `strings.json`): adds a ProOS **connection-health**
  binary_sensor (`<name> Connection`, `device_class=connectivity`,
  diagnostic). `on` = integration has a live connection; `off` = no connection.
  It is added unconditionally and is ALWAYS `available`, so it reports
  `off` even when the device is offline at startup — a stable signal the ProOS
  awareness/self-heal layer consumes instead of inferring health from the
  flapping media_player or the logs. Mirrors the existing keyboard-focus sensor
  pattern; no pyatv changes; uses the integration's existing per-device
  connect/disconnect dispatch.
- `entity.py` (proos.6, register 1031): the shared entity code listens for
  connect/disconnect by the DEVICE's identifier, not the entity's own id. Up to
  proos.5 the Connection sensor (whose id is `<id>_connection`) listened under a
  name the manager never sends, so it held its start-up reading — seen on
  Dave's box 27 Oct reading "connected" for two Apple TVs that had been
  disconnected since the day before. Bench:
  `apple_tv_connection_sensor_hears_the_driver_bench.py` (driven).
- `proos_extras.py` + hooks in `__init__.py`, `media_player.py`, `services.py`, `services.yaml`, `const.py`
  (proos.7, register 1034): the certified additions — the Apple TV's own display state (asleep / screensaver /
  idle / awake) and its installed apps by exact id as player attributes; actions with answers: proos_state
  (display, keyboard, profiles, now playing queue + language groups + chapter count, captions), proos_key
  (tap / double / hold / real press down-up with a 5 s safety release), proos_touch (swipe, press/hold/release,
  click), proos_media (skip by, scrub, chapters), proos_switch_account, proos_captions, proos_play_queue_item,
  proos_language_option. All through pyatv's own protocol objects; each says which pairing it needs when absent.
  Bench: `apple_tv_proos_extras_bench.py` (driven).
- `media_player.py` (proos.8, register 1037): artwork asked for 1600 wide (pyatv's default is 512 —
  Dave 27 Oct: "media for now playing is very low quality").
Everything else is byte-for-byte HA core 2026.6.3.

Because the domain stays `apple_tv`, this custom integration OVERRIDES the
built-in one, and the existing config entries (HomePods + Apple TVs) keep
working with no re-pairing.

## Compatibility (verified)
- pyatv 0.18.0 supports Python 3.14 (box runs 3.14.5).
- pyatv 0.18.0 has the SAME dependency floors as 0.17.0; HA 2026.6.3 already
  satisfies them all (cryptography 48.0.0, pydantic 2.13.4, protobuf 6.32.0,
  aiohttp 3.13.5, zeroconf 0.149.16) — so installing it swaps ONLY pyatv.
- Every pyatv symbol the integration imports (7 modules, 34 symbols) exists in
  0.18.0 — no API drift.

## Delivery
Bundle in the ProOS base image at `/config/custom_components/apple_tv/`, the
same model as the Samsung driver — it then rides inside proos-baseline. On the
next HA restart, HA installs pyatv 0.18.0 (replacing 0.17.0 in the venv) and
loads this integration. HA will log "custom integration apple_tv ... has not
been tested by Home Assistant" — that is expected for a certified ProOS build.

## Maintenance
When pyatv releases a newer fix, bump the `requirements` pin and the `version`
field. When HA core itself bumps to >= 0.18.0 and tvOS is stable, this fork can
be retired by deleting the folder.
