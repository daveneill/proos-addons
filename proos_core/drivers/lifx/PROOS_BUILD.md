# ProOS Certified LIFX Integration
A ProOS-certified fork of the platform's own `lifx`, REBUILT on Home Assistant 2026.10.2 (register 969b, 20 Oct 2026).
The first build (1.2.1+proos.1) was cut from 2026.6.3; the platform's own LIFX has since been rewritten (lifx-async, config
entries at VERSION 2), and on Dave's 2026.10.2 box the old fork could not read the entries the native one had made —
both LIFX entries showed migration_error. Supported by Protech.

Changes vs native 2026.10.2: manifest `version` (`<lifx-async>+proos.2`) and `@protech-proos`; `binary_sensor.py` adds a
ProOS connection-health sensor (`<name> Connection`, connectivity, diagnostic), always available, on = the per-device
coordinator's last update succeeded. Everything else byte-for-byte native 2026.10.2. Domain stays `lifx`, so this
replaces the platform's own — rebuild it whenever the platform changes its LIFX.
