# ProOS Certified LIFX Integration

A ProOS-certified fork of the Home Assistant core `lifx` integration. Supported by
Protech; native library integrations are third-party and unsupported.

## What this build changes (vs HA core 2026.6.3 `lifx`)
- `manifest.json`: added `version` (`1.2.1+proos.1`) and `@protech-proos` codeowner.
  Nothing else changed (same aiolifx requirements, same dhcp/homekit discovery, same
  domain).
- `binary_sensor.py`: adds a ProOS **connection-health** binary_sensor
  (`<name> Connection`, `device_class=connectivity`, diagnostic). Added unconditionally
  in `async_setup_entry` and ALWAYS available, so it reports `off` even when the bulb is
  offline at startup. `on` = the per-device `LIFXUpdateCoordinator`'s last update
  succeeded. `lifx` already loads the `binary_sensor` platform (HEV cycle), so no
  `__init__.py` change was needed — the sensor rides the existing platform.

Everything else is byte-for-byte HA core 2026.6.3. Domain stays `lifx`, so this
overrides the built-in integration; existing LIFX entries keep working.

## Awareness wiring (Core)
Point the bulb's ProOS reachability at `binary_sensor.<name>_connection` (state
`on`/`off`).

## Delivery / maintenance
Overlay these two files onto the native `lifx` folder for HA 2026.6.3 in
`/config/custom_components/lifx/`. Re-base and bump `+proos.N` when core changes the
`lifx` files; retire by deleting the folder if HA ships an equivalent signal.
