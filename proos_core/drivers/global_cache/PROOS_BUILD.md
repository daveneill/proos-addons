# ProOS Global Caché — a ProOS driver (register 1323, 12 Nov 2026)

Dave, 12 Nov 2026: "If you can build it then I will order one to test with" — and "It needs to support the way the
platform is supporting the other rack one … and support all the models".

- Written by ProOS to the maker's documents (Dave's certification standard, 3 Nov): Global Caché Unified TCP API v1.1
  (API-GC-UnifiedTCPv1.1.pdf), the iTach API 1.5 (discovery beacon), the Global Connect TCP API 1.0 (module numbering).
  No upstream: the platform's own `itach`/`gc100` are legacy YAML-only; a modern IP2IR integration was proposed to the
  platform and closed; the one community driver covers iTach IR only, without the platform's infrared entities.
- The platform's way, nothing rebuilt: IR ports are the platform's InfraredEmitterEntity/InfraredReceiverEntity
  (as Broadlink and ESPHome are), so every platform infrared integration sends through them; serial ports join the
  platform's serial port list through usb.async_register_serial_port_scanner (as ESPHome's serial proxy does), as
  socket://<unit>:4999…, so every platform RS-232 integration can pick them; relays are switches; sensor inputs are
  binary sensors; the Global Connect HDMI switch is a select. Found by the platform's DHCP matcher (MAC 00:0C:1E) and
  the units' own AMXB beacon; or typed.
- Models: iTach IP2IR/WF2IR/IP2SL/WF2SL/IP2CC/WF2CC, iTach Flex, GC-100-06/-12/-18, Global Connect IR/SL/RT/HM modules
  (each Global Connect module is its own unit and its own entry, as the maker built it).
- Benched: core/tests/global_cache_speaks_every_family_bench.py — the real protocol.py over real TCP against a stand-in
  of every family, with a real LG code from the platform's own infrared_protocols library.
- NOT YET SEEN on a unit: everything. Dave is ordering one. Working = his box sends a platform IR command through it,
  an RS-232 integration talks through its serial port, a relay switches — each with the reading shown.
- Version 1.0.0+proos.1.
