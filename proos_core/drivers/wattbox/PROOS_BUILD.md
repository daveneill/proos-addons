# ProOS WattBox — a community driver carried by ProOS (not certified)

Register 1185. Dave, 3 Nov 2026: "not every site will have a wattbox thats why we had the recovery section that we can
allocate devices that the room has" — and "yes" to a ProOS WattBox driver, so an outlet (his Family Room TV's) can be
chosen as that device's power device in Pro › room › Device Recovery. Under the 16/20 Oct rulings (ProOS ships its own
drivers inside Core; a non-certified integration is labelled as such) this one rides in Core and is NOT certified.

- Upstream: github.com/eseglem/hass-wattbox, tag v1.3.0 (commit 6c6eaf0). MIT licence, kept as LICENSE.upstream.
- Byte-for-byte upstream EXCEPT manifest.json: `codeowners` → `@protech-proos`, `version` 1.3.0 → 1.3.0+proos.1.
  No functional change. Domain stays `wattbox`; name "WattBox".
- What it gives: one switch per outlet (on/off), a reset (power-cycle) button per outlet, unit sensors; per-outlet
  metering on telnet/SSH only. Series: 150/250/300/700 over HTTP (port 80); 800 over telnet (port 23).
- Set up in the platform's own UI (Add Integration → WattBox): the WattBox's address, connection type, and its own
  login (default wattbox/wattbox). "Skip outlets matching" keeps an outlet (the router's) from ever getting a switch.
- First load installs its requirements on the box (asyncssh, getmac).
- Nothing is claimed until read on the box: the outlet's switch appearing, and a recovery bound to it reading back.
