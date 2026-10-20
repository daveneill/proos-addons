# ProOS AV Bridge 2.0 — Savant profile (register 631, 28 Sep 2026)

Written to Savant's Component Profile Developer's Guide (Parts 1 and 2). An IP
device profile: the Savant host connects OUT to the ProOS box on tcp/25804 and
parses the lines ProOS sends. Nothing is installed on the Savant host.

The dealer downloads this file from **Pro › Systems › Savant** (Core serves it,
so the profile and the feed can never disagree). File name must stay
`proos_avbridge.xml` (Blueprint places a user profile by
`<manufacturer>_<model>.xml`, lower case).

## Install (Savant dealer, one visit)
1. Pro › Systems › Savant: switch the feed on; download the profile.
2. Blueprint › Preferences › Libraries › Import `proos_avbridge.xml`.
   It appears as **ProOS / AVBridge "ProOS AV Bridge 2.0"**.
3. Place it. IP address = the ProOS box (shown on the Pro page). Port 25804.
4. Upload. Pro's rows go green when the host connects and starts polling.
5. Wire state triggers per room, e.g.
   `Activity_living_room == watch_apple_tv` → set the room's virtual TV / service;
   `Activity_living_room == off` → room off; `TVPowerStatus_living_room` for the TV.
6. To let ProOS see what a Savant remote did: from a state trigger on the zone's
   own service/state, run the component's **ReportZone** action with
   Zone = the zone name, Key = `service`, Value = the state. ProOS mirrors it as
   `sensor.proos_savant_<zone>` and shows it on the Pro page.

## What it needs from ProOS
Core 1.0.797 or later with the feed switched on in Pro. Room ids are the ones
ProOS publishes (lower case, underscores) and are listed on the Pro page.
