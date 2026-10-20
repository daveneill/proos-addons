# ProOS Certified HEOS Integration
ProOS-certified fork of HA core `heos` (2026.6.3). Supported by Protech.
Changes vs native: manifest `version` (1.0.6+proos.1) + `@protech-proos`; `__init__.py`
adds `Platform.BINARY_SENSOR`; new `binary_sensor.py` adds a ProOS connection-health
sensor per player (`player.available`, coordinator-driven, always-available) for the
awareness layer. Everything else byte-for-byte native. Domain stays `heos`.
