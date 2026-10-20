# ProOS Certified WiZ Integration

A ProOS-certified fork of the Home Assistant core `wiz` integration. Supported by
Protech; the stock/native library integrations are third-party and unsupported.

## Why this exists
WiZ bulbs are a certified ProOS lighting class (discrete on/off, level, colour). The
native integration reports device state well but gives the ProOS awareness/self-heal
layer no *stable* liveness signal — the light entities simply go `unavailable` when a
bulb drops, which is indistinguishable from "off at startup" and flaps. This build
adds a dedicated connection-health signal ProOS can watch and recover against.

## What this build changes (vs HA core 2026.6.3 `wiz`)
- `manifest.json`: added `version` (`0.6.3+proos.1` — custom components require a
  version field) and added `@protech-proos` to `codeowners`. Nothing else changed —
  same `requirements` (`pywizlight==0.6.3`), same dhcp discovery, same domain.
- `binary_sensor.py`: adds a ProOS **connection-health** binary_sensor
  (`<name> Connection`, `device_class=connectivity`, diagnostic). Added
  unconditionally in `async_setup_entry` and ALWAYS `available`, so it reports `off`
  even when the bulb is offline at startup. `on` = the coordinator's last update
  succeeded; `off` = it did not. Keyed off the integration's existing
  `WizCoordinator`, so it costs nothing extra and needs no pywizlight change.

Everything else is byte-for-byte HA core 2026.6.3.

Because the domain stays `wiz`, this custom integration OVERRIDES the built-in one,
and existing WiZ config entries keep working with no re-pairing.

## Delivery
Bundle in the ProOS base image at `/config/custom_components/wiz/` (the same model as
the Apple TV and Samsung drivers) so it rides inside proos-baseline. Assemble the
shipped folder by copying the native `wiz` folder for HA 2026.6.3 and overlaying the
two files in this bundle (`manifest.json`, `binary_sensor.py`). On the next HA restart
HA loads this integration; the log line "custom integration wiz ... has not been
tested by Home Assistant" is expected for a certified ProOS build.

## Awareness wiring (Core)
The watcher's second signal for a WiZ bulb becomes `binary_sensor.<name>_connection`
(state `on`/`off`), a cleaner reachability input than the IP probe. Point the device's
reachability at this sensor, or let netmap pick it up.

## Maintenance / retire
When HA core changes the `wiz` files, re-overlay the two ProOS files onto the new
native version and bump `+proos.N`. If HA ever ships an equivalent connectivity
signal, this fork can be retired by deleting the folder.
