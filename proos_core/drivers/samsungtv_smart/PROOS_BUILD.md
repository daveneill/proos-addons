# SamsungTV Smart — ProOS patch build (register 1310)

The author's own release (github.com/TheFab21/ha-samsungtv-smart, see PROOS_UPSTREAM) plus ProOS's written changes
(LIVE/tools/drivers/samsungtv_smart/proos.patch), rebuilt with tools/rebuild_driver.py. Not a frozen copy.

By 9.3.0 the author had absorbed what ProOS's 8.0.0 copy added — local IP-Control inputs (verified, before
SmartThings), the 1515/1516 port, Frame art-mode readback, accept-on-the-press input switching — under the same saved
setting names. The patch keeps only what 9.3.0 lacks:
1. STANDARD_IP_SOURCE_MAP — a TV paired for IP Control shows "HDMI 1 … HDMI 4 | TV", driven locally; every ProOS
   activity names these inputs. SmartThings does not replace them.
2. ip_tuner_token — a per-panel tuner token (blank = "TV"), set in Reconfigure → IP Control; "TV" is translated
   before it is sent (the Family Room Frame landed "TV" on the smart hub, 2 Aug 2026).
Every edit is tagged "ProOS".
