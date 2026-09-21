"""ProOS Music -- the engine names itself to speakers and their apps as
"ProOS Music" (register 558, 21 Sep 2026).

Dave: "I cant have Music Assistant show in native client apps."

WHERE THE NAME CAME FROM (read in the 2.9.5 source, tag 2.9.5, not assumed):
the Sonos app prints the SERVICE name the engine hands Sonos's cloud queue
under the station or track -- "Roots Radio / Music Assistant" in Dave's
screenshot -- and every plain HTTP stream carries the name in its icy headers.
None of it is a setting; it is written into the code. So it is changed here,
in our image, the same way proos.1-3 carry their fixes.

WHAT IS CHANGED: only the words the engine SHOWS to a speaker or an app.
WHAT IS NOT: APPLICATION_NAME itself. The engine also uses it to introduce
itself to streaming services and image hosts (user agents), and changing
that risks their logins for a cosmetic gain. The service ID "mass" stays too:
Sonos routes the cloud queue by that id, and it is never displayed.

EVERY EDIT IS COUNTED. If upstream moves or rewords any of these, the BUILD
FAILS here rather than shipping an image that quietly shows their name again.
"""
import pathlib
import sys

OURS = "ProOS Music"
THEIRS = "Music" + " Assistant"   # spelled once, only to find it

# (file, exact text, replacement, how many times it must appear)
# ORDER MATTERS in sonos/provider.py: the one-line service dict contains the
# plain `"name": ...,` text as a substring, so it is replaced first.
EDITS = [
    ("providers/sonos/provider.py",
     '"service": {"name": "%s", "id": "mass"}' % THEIRS,
     '"service": {"name": "%s", "id": "mass"}' % OURS, 2),
    ("providers/sonos/provider.py",
     '"name": "%s",' % THEIRS, '"name": "%s",' % OURS, 1),
    ("providers/sonos/player.py",
     '"name": "%s",' % THEIRS, '"name": "%s",' % OURS, 1),
    ("constants.py",
     '"Server": APPLICATION_NAME,', '"Server": "%s",' % OURS, 1),
    ("constants.py",
     '"icy-name": APPLICATION_NAME,', '"icy-name": "%s",' % OURS, 2),
    ("constants.py",
     '"icy-description": f"{APPLICATION_NAME} - Your personal music assistant",',
     '"icy-description": "%s",' % OURS, 1),
    ("controllers/streams/controller.py",
     'title = "%s"' % THEIRS, 'title = "%s"' % OURS, 1),
    ("controllers/streams/controller.py",
     'headers={"icy-name": "%s"}' % THEIRS,
     'headers={"icy-name": "%s"}' % OURS, 1),
    ("providers/chromecast/player.py",
     'title = current_media.title or "%s"' % THEIRS,
     'title = current_media.title or "%s"' % OURS, 1),
]


def main(pkg):
    root = pathlib.Path(pkg)
    files = {}
    for rel, old, new, want in EDITS:
        p = root / rel
        s = files.get(rel)
        if s is None:
            s = p.read_text(encoding="utf-8")
        got = s.count(old)
        if got != want:
            sys.exit("proos brand patch: %s expected %d of %r, found %d"
                     % (rel, want, old, got))
        files[rel] = s.replace(old, new)
    for rel, s in files.items():
        (root / rel).write_text(s, encoding="utf-8")
    print("proos brand patch: %d edits in %d files" % (len(EDITS), len(files)))


if __name__ == "__main__":
    main(sys.argv[1])
