# ProOS Certified Denon / Marantz Integration
ProOS-certified fork of HA core `denonavr` (2026.6.3). Supported by Protech.
Changes vs native: manifest `version` (1.3.2+proos.1) + `@protech-proos`; `__init__.py`
adds `Platform.BINARY_SENSOR`; new `binary_sensor.py` adds a ProOS connection-health
sensor (`<name> Connection`, connectivity, diagnostic) that mirrors the media_player's
availability (denonavr has no coordinator) but stays always-available for the awareness
layer. Everything else byte-for-byte native. Domain stays `denonavr`.
