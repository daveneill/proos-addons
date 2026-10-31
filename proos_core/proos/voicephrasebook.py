"""MORE WAYS OF SAYING WHAT THE PLATFORM ALREADY DOES — ProOS's own phrasebook for the platform's built-in commands
(register 1133; 1 Nov 2026).

Dave, 09:40: "Yes I want all those as above made instant" — the list in docs/ProOS_Voice_Instant_vs_AI_2026-11-01.md.

READ ON THE BOX (the platform's own recogniser, conversation/agent/homeassistant/debug — reads, never acts):
  "set the office brightness to 30"      → HassLightSet, Office, 30      (the platform knows how to DO it)
  "set the bedroom lights to 50%"        → no match                      (but not how most people SAY it)
  "dim the family room lights to 30 percent" → no match
Each miss went to the cloud AI: 10–17 seconds for something the house can do in under one.

THE PLATFORM FIRST (CLAUDE.md, "if the platform already does it"). The platform has its own place for extra ways of
saying its built-in commands: a sentences file in <config>/custom_sentences/<language>/, loaded with its library and
re-read by its own conversation.reload. Its own command does the work (HassLightSet sets the room's lights) — ProOS
adds only words. Core writes ONE file, custom_sentences/en/proos.yaml; the platform reports a match from it as
source "custom", file "en/proos.yaml" (read in the platform's own code, default_agent.py), and direct.py treats that
file exactly like the built-in library — nothing else written there is ProOS's to act on.

The file is written as JSON (every JSON file is YAML; Core ships no YAML library), under one comment line that carries
its fingerprint. An installer who edits the file changes the fingerprint and Core leaves it alone from then on, the same
rule as every sentence automation ProOS writes (project.voice_write).

THIS BUILD: brightness by percent in a room. The next builds add their own sentences to PHRASES, one at a time.
"""
from __future__ import annotations

import hashlib
import json
import os

FILE_NAME = "proos.yaml"
LANGUAGE = "en"
MARK = "# Written by ProOS Core — more ways of saying the platform's own commands. Edit it and Core leaves it alone. proos_hash: "

# The words. {area}, {brightness}, <light>, <the>, <in>, <all>, <numeric_value_set> are the platform library's own lists
# and rules ("set|change|make|turn" … "light|lights|lighting"); nothing here invents a list of its own.
PCT = "{brightness}[([ ]%)| percent]"
PHRASES = {
    "HassLightSet": [
        {"sentences": [
            # "set the bedroom lights to 50%", "dim the family room lights to 30 percent", "turn the kitchen lights down to 20%"
            "[<numeric_value_set>|dim|brighten|turn] [<the>] {area} <light> [up|down] to " + PCT,
            # "set the lights to 40% in the bedroom" — and the room-page form Assist makes from "set the lights to 40%"
            "[<numeric_value_set>|dim|brighten|turn] [(<all>|<the>)] <light> [up|down] to " + PCT + " [<in>] [<the>] {area}",
            # "dim the bedroom to 30%"
            "(dim|brighten) [<the>] {area} to " + PCT,
            # "dim the lights in the office to 30%"
            "(dim|brighten) [(<all>|<the>)] <light> [<in>] [<the>] {area} to " + PCT,
            # "bedroom lights to 50%", "office lights at 30 percent"
            "[<the>] {area} <light> (at|to) " + PCT,
        ], "slots": {"domain": "light"}},
    ],
}


def phrasebook() -> dict:
    """The sentences file's contents (as the platform reads it)."""
    return {"language": LANGUAGE, "intents": {k: {"data": v} for k, v in PHRASES.items()}}


def _body(book: dict) -> str:
    return json.dumps(book, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def render(book: dict = None) -> str:
    body = _body(book if book is not None else phrasebook())
    return MARK + _hash(body) + "\n" + body


def config_dir() -> str:
    """The platform's config folder as Core sees it (homeassistant_config is mounted at /homeassistant)."""
    return "/homeassistant" if os.path.isdir("/homeassistant") else "/config"


def path_in(base: str) -> str:
    return os.path.join(base, "custom_sentences", LANGUAGE, FILE_NAME)


def is_ours(rec) -> bool:
    """A recogniser result from THIS file (source "custom", file "en/proos.yaml")."""
    if not isinstance(rec, dict) or rec.get("source") != "custom":
        return False
    return os.path.basename(str(rec.get("file") or "").replace("\\", "/")) == FILE_NAME


def write(client=None, base: str = None) -> dict:
    """Write the file when it differs and ask the platform to re-read its sentences. An edited file is left alone."""
    p = path_in(base or config_dir())
    want = render()
    try:
        with open(p, encoding="utf-8") as f:
            cur = f.read()
    except FileNotFoundError:
        cur = None
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if cur is not None:
        if cur == want:
            return {"ok": True, "action": "unchanged", "path": p}
        first, _, rest = cur.partition("\n")
        if not first.startswith(MARK) or first[len(MARK):].strip() != _hash(rest):
            return {"ok": True, "action": "kept_edited", "path": p}
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(want)
        os.replace(tmp, p)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    reloaded = False
    if client is not None:
        try:
            client._req("POST", "/api/services/conversation/reload", {})
            reloaded = True
        except Exception as e:                                   # noqa: BLE001
            return {"ok": True, "action": "written", "path": p, "reloaded": False, "error": str(e)}
    return {"ok": True, "action": "written", "path": p, "reloaded": reloaded}
