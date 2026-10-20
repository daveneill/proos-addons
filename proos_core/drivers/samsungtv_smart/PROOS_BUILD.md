# SamsungTV Smart — ProOS build

A ProOS-maintained fork of [`TheFab21/ha-samsungtv-smart`](https://github.com/TheFab21/ha-samsungtv-smart)
(v8.0.0) that adds **local, SmartThings-free discrete input switching** over
Samsung's port-1516 JSON-RPC channel — the same local control path certified
Control4/Crestron drivers use.

## Why this exists

The upstream integration can only switch discrete HDMI inputs two ways:
sending `KEY_HDMIn` remote codes (ignored by many Tizen panels, including the
2019–2020 Frame/QLED sets), or through the **SmartThings cloud**
(`ST_HDMIn` / `samsungvd.mediaInputSource`). On TVs where the discrete keys are
dead, that forces a Samsung-account cloud dependency per install.

This build adds a third path that needs neither: it drives Samsung's
`inputSourceControl` JSON-RPC method on the **already-paired IP Control channel
(port 1516)**. Local, deterministic, with a confirmed input read-back — exactly
how Control4 does it.

## What changed (purely additive — no upstream logic altered)

- **`api/ipcontrol.py`**: two methods on the existing `SamsungIPControl` client —
  `async_set_input_source("HDMI1")` (drives `inputSourceControl`) and
  `async_get_input_source()` (reads `inputSource` from `getTVStates`).
- **`media_player.py`**: a new `IP_` source-key branch in the `_async_send_keys`
  dispatcher routes to the IP client; `_get_source` reports the IP-selected
  input so the card reflects reality; `_resolve_source_by_id` accepts the bare
  technical ID (e.g. `HDMI1`).
- **`manifest.json`**: build identity (`name`, `version 8.0.0+proos.1`, codeowner).

The domain stays `samsungtv_smart`, so this is a drop-in replacement — entity
IDs, automations, art-mode, power, and the SmartThings/KEY paths all keep
working unchanged. Every edit is tagged with a `ProOS` comment for easy
re-application when tracking upstream.

## Prerequisites

IP Control must be **paired and enabled** on the TV (the same channel the build
uses for reliable power/art-mode):
- TV setting: *Settings → Connections → Network → Expert Settings → IP Remote* (on).
- In HA: integration card → **Reconfigure → IP Control** → pair (TV on, on a
  normal input, accept the on-screen prompt).

No SmartThings account is required for input switching.

## Configuration

Define discrete inputs in the integration's **Source list** option using `IP_`
keys mapped to the TV's raw input strings (`HDMI1`–`HDMI4`, `TV`, …):

```json
{
  "Apple TV": "IP_HDMI1",
  "Shield":   "IP_HDMI2",
  "Foxtel":   "IP_HDMI3",
  "TV":       "IP_TV"
}
```

(The HDMI-number-to-device mapping is per install — set it to match how each
site is patched.)

## Usage

```yaml
# By display name
action: media_player.select_source
target: { entity_id: media_player.family_room_tv }
data:   { source: "Apple TV" }

# Or by the raw technical ID
data:   { source: "HDMI1" }
```

Mixed key chains work too (e.g. `"IP_HDMI1+1500+KEY_..."`), and `IP_` keys can
sit anywhere a source key is accepted.

## Verify (state-before-control)

`async_set_input_source` returns the input the TV reports landing on, and
`_get_source` surfaces the IP-selected input as the media_player `source`
attribute — so a ProOS activity can confirm the switch took rather than assume.

## Install

- **Single box**: copy `custom_components/samsungtv_smart/` over the existing
  folder and restart HA. (If migrating from stock upstream, remove it from HACS
  first so HACS doesn't overwrite this build on update.)
- **Fleet build**: bundle this `custom_components/samsungtv_smart/` in the ProOS
  base image; commissioning pairs IP Control and writes the per-TV source list.

## Notes & next steps

- **Tracking upstream**: the patch is ~105 added lines across two files, all
  `ProOS`-tagged. Re-applying onto a newer upstream release is mechanical;
  `proos-samsungtv-input.patch` is the unified diff.
- **Live read-back (optional next)**: `source` currently reflects the last
  ProOS-driven input. Polling `getTVStates.inputSource` on the existing IP
  refresh loop would also catch input changes made by the physical remote —
  deliberately deferred to keep this build's diff minimal and the art-mode poll
  untouched.
- **Full domain rename (optional)**: kept as `samsungtv_smart` for drop-in
  compatibility. A fully separate `proos_tv` domain is possible later but
  touches unique_ids/services/translations and forces re-pairing — not worth it
  for v1.

## 8.0.0+proos.2 — IP Control port auto-probe (Frame 1516 / QLED 1515)

Samsung exposes the licensed IP Control JSON-RPC on different TCP ports across
model generations: 2020+ Frames on **1516**, 2019 QLEDs (Q-series) on **1515**.
The previous build hardcoded 1516, so pairing a QLED hit a dead port and failed
with `connection refused`.

Change (purely additive):
- `api/ipcontrol.py`: `IP_CONTROL_PORT_CANDIDATES = (1516, 1515)`; new
  `SamsungIPControlPortClosedError` (raised on `ConnectionRefusedError`) so a
  closed port is distinguishable from a TV that answered but declined pairing;
  new `async_pair_autoprobe()` that tries each candidate port in order and pairs
  on the first that answers.
- `config_flow.py`: IP-Control pairing uses `async_pair_autoprobe`; the detected
  port is persisted to `entry.data[CONF_IP_CONTROL_PORT]`.
- `const.py`: `CONF_IP_CONTROL_PORT` entry-data key.
- `media_player.py`, `number.py`, `switch.py`, `button.py`, `select.py`: every
  IP Control client now reads the persisted port (default 1516 when absent, so
  Frames already paired keep working).

Net: pairing self-detects the port. Installer never picks 1516 vs 1515.

## 8.0.0+proos.3 — standard discrete-input map as the default for paired TVs

A paired TV now exposes the ProOS standard discrete-input source list with no
configuration: HDMI 1, HDMI 2, HDMI 3, HDMI 4, TV — each routed over IP Control.
No options-flow paste, no per-TV setup; applies to every IP-paired TV on restart
(existing and future).

- `const.py`: `STANDARD_IP_SOURCE_MAP` ({"HDMI 1":"IP_HDMI1", ... "TV":"IP_TV"}).
- `media_player.py`: `_load_tv_lists` uses the standard map as the source list
  when no explicit `source_list` is configured AND the TV is IP-paired (else the
  legacy KEY_-code default). New `_is_ip_control_paired()` helper.
- `media_player.py`: `_get_st_sources` now yields to IP Control — local discrete
  input takes precedence over the SmartThings cloud source list, so the standard
  map is not clobbered on SmartThings-connected TVs.

Precedence: explicit options source_list > IP-paired standard map > SmartThings
sources > legacy TV/HDMI default.

Note: the tuner token `IP_TV` is provisional — HDMI1-4 are proven; confirm the
tuner switches on first live test and adjust the "TV" mapping if the firmware
expects a different inputSource string (e.g. "dtv").

## 8.0.0+proos.4 — hotfix: _is_ip_control_paired must not use self.hass at init

proos.3 regressed setup: `_is_ip_control_paired()` read the token via
`self.hass.config_entries`, but it is reached from `_load_tv_lists` during
`__init__` — before the entity is added to hass, so `self.hass` is None. That
raised `'NoneType' object has no attribute 'config_entries'`, failing media_player
platform setup and marking every entity unavailable.

Fix: read the token hass-free from the entry config snapshot
`self._entry_data[DATA_CFG]` (a copy of entry.data, which carries
CONF_IP_CONTROL_TOKEN), mirroring how `_get_option` reads DATA_OPTIONS. Safe at
__init__ and at runtime; re-pairing reloads the entry and refreshes the snapshot.

## 8.0.0+proos.8 — 2026-08-02
A detected Frame defaults its own Art Mode readback ON (detection, not
selection — no Frame/standard integration split). Three touch points:
* `__init__.py` setup: absent `ip_control_art_mode` on a known Frame is
  filled True at load (heals every existing install on upgrade/reload).
* `config_flow.py` reconfigure_ip_control: form default AND write fallback
  key off `entry.data[is_frame_tv]` — a flow re-run can never regress a
  working Frame back to False (the recurring prepare-alarm root cause).
* `sensor.py`: the moment the Frame probe first confirms, the option is
  filled alongside the persisted flag.
An installer's explicit False always wins — only ABSENT is defaulted.
Standard (non-Frame) Samsungs keep the False default: nothing to read.

## 8.0.0+proos.9 — 2026-08-02
The tuner is not the platform. Selecting "TV" over IP Control lands some
panels on the Samsung smart platform, NOT the broadcast tuner (observed on
the Family Room Frame). Two additions, evidence-first:
* `ip_input_source` attribute — the live input as the panel itself reports
  it (getTVStates.inputSource), refreshed with the art-mode poll. Put the
  panel on the REAL tuner with the native remote and read this attribute:
  that string is the panel's discrete tuner token.
* `ip_tuner_token` option (Reconfigure → IP Control) — when set, a "TV"
  source select is translated to this token before it hits the wire. The
  committed source stays "TV" for Core/Pro/generator; the translation is
  the integration's job. Blank = unchanged behaviour.
