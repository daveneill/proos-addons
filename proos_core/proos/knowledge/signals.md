# Reading a device: which signal means what

Learned from real failures on real installs. These are readings, not
rules about brands — they hold because of how the platform presents
devices, not because of a badge on the box.

## The media_player entity is the device's AV state
off · standby · idle · playing. This is the one to read when someone
asks what a device is doing.

## A remote entity, a device_tracker or a cast link is CONNECTIVITY
It is connectivity, not power. It reads "on" whenever the device is
REACHABLE — standby included — so it stays on while the thing is
actually off. Never say an Apple TV, a
Shield or a Chromecast is on because its remote says so. If someone
asks why the remote shows on while the TV is off: the connection is
alive, the device is not on.

Recorded 7 Aug 2026, after ProOS read a remote as power and got it
backwards.

## Standby is a resting state, not a fault
TVs and streamers sleep. A device in standby is normal and is never
reported as a problem.

## A device that does not exist is not a broken device
If a state reads missing — "no such entity on this box" — the id is
wrong or stale, not the device. Search for the real id and read that.
Only an entity that EXISTS and reports unavailable is a device with a
problem.

## A switched-off device stays on the network
So a device reporting off whose independent witness says GONE is dead,
unplugged, or cut off — that is a different fault from "someone turned
it off", and it is worth saying which.

## Some drivers cannot see what is on screen
Certain streamer integrations sit at "idle" while the thing is happily
playing. A raw state is ONE witness, not the machine.
