# ProOS Certified Sonos Integration
ProOS-certified fork of HA core `sonos` (2026.6.3). Supported by Protech.
Changes vs native: manifest `version` + `@protech-proos`; extended the existing
`binary_sensor.py` with a per-speaker ProOS connection-health sensor
(`speaker.available`, connectivity, diagnostic, always-available), created for every
speaker via the SONOS_CREATE_MEDIA_PLAYER signal and pushed by the SONOS_STATE_UPDATED
dispatcher. Everything else byte-for-byte native. Domain stays `sonos`.
