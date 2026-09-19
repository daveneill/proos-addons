# Building a moment, and proving it

## Build it the way you would by hand
When someone asks for something the house should be able to do and no
ProOS tool fits, do not bend a tool that does not fit and do not
describe what you would need. Build it:

1. **READ** the devices involved — what is this speaker actually
   playing, what does it report it can do.
2. **WRITE** the script from those exact readings.
3. **RUN** it.
4. **READ THE DEVICES BACK** and see whether it did what was asked.
5. If it did not — it loaded but is still paused, the volume did not
   move — **FIX IT AND RUN IT AGAIN.**

Only then say it works, and say what you read.

**A saved thing you have not run is NOT tested.** Never call it working.

Recorded 16 Aug 2026, after a Developer session did exactly this by
hand in four steps with the platform's own tools, and ProOS could not.

## A moment can be music only
A scene with no lights in it is a legitimate moment. Requiring device
states forced the invention of a scene containing three switches nobody
asked for.

## The proven script becomes the scene's companion
Attach it as the scene's activity companion so a tile tap replays
exactly what was proven. Removing the TV or source from a scene means
removing that companion; removing its music means removing the music
companion.

## Scenes store what they are given
A scene is stored exactly as sent — nothing converted, stripped or
added, and nothing in ProOS decides what a device can do. That is why
the read-back is not optional: if something sent did not land, the
devices say so, and it is fixed there.

An id that is not on the box is refused. Read the room's real devices
and use those exact ids.
