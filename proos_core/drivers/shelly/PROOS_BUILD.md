# ProOS Certified Shelly Integration
ProOS-certified fork of HA core `shelly` (2026.6.3). Supported by Protech.
Changes vs native: manifest `version` + `@protech-proos`; extended the existing
`binary_sensor.py` async_setup_entry to also add a ProOS connection-health sensor on the
active coordinator (RPC for Gen2+, Block for Gen1) keyed off `last_update_success`,
always-available, for the awareness layer. Everything else byte-for-byte native. Domain
stays `shelly`.
