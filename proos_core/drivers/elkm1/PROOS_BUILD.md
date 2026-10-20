# ProOS Certified Elk M1 Integration
ProOS-certified fork of HA core `elkm1` (2026.6.3). Supported by Protech.
Changes vs native: manifest `version` (2.2.15+proos.1) + `@protech-proos`; extended the
existing `binary_sensor.py` with a panel-level ProOS connection-health sensor
(`elk.is_connected()`, connectivity, diagnostic, always-available) on the panel system
device. Security is not power-aware, so a lost panel is a high-priority awareness signal.
Everything else byte-for-byte native. Domain stays `elkm1`.

## White-label rebrand (Ness M1)
`manifest.json` `name` is set to **Ness M1** (the Australian brand for Elk M1).
Domain stays `elkm1`, so this still overrides the built-in integration and existing
entries keep working with no re-pairing. The ProOS dashboard shows the Ness logo and
"Ness M1" via its own bundled brand map — it does NOT pull this logo from Home
Assistant's brands CDN.
