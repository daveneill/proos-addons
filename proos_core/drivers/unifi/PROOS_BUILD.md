# ProOS Certified UniFi Network Integration

A ProOS-certified fork of the Home Assistant core `unifi` (UniFi Network)
integration. Part of the ProOS **UniFi Certified** suite (alongside UniFi
Protect). Supported by Protech; the third-party library integration is
unsupported.

## What this build changes (vs HA core 2026.6.3 `unifi`)
- `manifest.json`: added `version` (`91+proos.1`, tracking `aiounifi==91`) and the
  `@protech-proos` codeowner.
- `const.py`: added `Platform.BINARY_SENSOR` to `PLATFORMS`.
- `binary_sensor.py` (new): a ProOS **connection-health** sensor on the UniFi
  Network controller device (`Connection`, `device_class=connectivity`, diagnostic).
  It reflects `hub.available` (the websocket state the integration itself uses) and
  updates on the hub's `signal_reachable` dispatcher, but stays ALWAYS available so
  it reports `off` when the controller drops — a stable liveness signal for the
  ProOS awareness/self-heal layer.

Everything else is byte-for-byte HA core 2026.6.3 (from your upload). Domain stays
`unifi`, so this overrides the built-in integration; existing entries keep working
with no re-pairing.

## Why UniFi Network matters to ProOS (awareness · monitoring · recovery)

**Awareness (second signals).** UniFi sees devices independently of their own
vendor integration:
- per-client `device_tracker` (home/away) → a reachability second-signal for ANY
  networked device;
- per-port link state and PoE-power sensors → confirms a device is physically
  drawing power;
- WAN / uplink sensors → a backbone signal, so an internet drop doesn't false-alarm
  every cloud device at once.

**Recovery (native, no smart plug needed).** The integration already exposes the
exact hardware-recovery controls ProOS wants, as `button` entities:
- **PoE power cycle** — per PoE port (`unique_id` `power_cycle-<port>`,
  translation_key `port_power_cycle`). A single press power-cycles whatever is on
  that port (a hung camera, AP, or any PoE device). Available only where
  `port_poe` / `poe_enable` is true.
- **Device restart** (`unique_id` `device_restart-<device>`) — reboots a UniFi
  device (AP / switch); a softer recovery.

ProOS recovery consumes these via `button.press` — cleaner and safer than a manual
switch off/on. UniFi knows which switch port each wired client is on (client
`sw_mac` / `sw_port`), so Core can **auto-map** a device → its PoE-power-cycle
button rather than making the installer hunt for it.

## Awareness / recovery wiring (Core)
- Reachability second signal: point at `binary_sensor.<controller>_connection` for
  the controller, and at the relevant UniFi client `device_tracker` for a device.
- New recovery method `poe_cycle`: press the mapped `button` PoE-power-cycle entity.
- New recovery method `device_restart`: press the mapped `button` restart entity.
- Both surface to the installer (per-device Recovery config) and to ProAssist as a
  consent-gated, tiered tool.

## Maintenance / retire
Re-base onto a newer native `unifi` and bump `+proos.N` when core changes. Retire by
deleting the folder if HA ships an equivalent controller connectivity signal.
