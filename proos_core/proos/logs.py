"""
ProOS Core — log access (tech tier).

Surfaces the logs an installer would otherwise open native HA for: Home
Assistant Core, the Supervisor, the host, and every installed add-on — all via
ProCore's SUPERVISOR_TOKEN, so the Tech Tools box is a full diagnostic surface
and the installer never touches Home Assistant. The calling route enforces
tech/owner identity; this module trusts it is gated. Output is ANSI-stripped,
size-capped, and Supervisor-token-redacted.
"""
from __future__ import annotations
import json
import os
import re
import urllib.request

SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
MAX = 80000
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Fixed journal sources the Supervisor exposes.
_SOURCES = {
    "core": "/core/logs",
    "supervisor": "/supervisor/logs",
    "host": "/host/logs",
    "audio": "/audio/logs",
    "dns": "/dns/logs",
    "multicast": "/multicast/logs",
}


def _get(path: str, accept: str = "text/plain") -> bytes:
    req = urllib.request.Request(SUPERVISOR + path, method="GET")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read()


def _clean(txt: str) -> str:
    txt = _ANSI.sub("", txt)
    if TOKEN:
        txt = txt.replace(TOKEN, "«redacted»")
    return txt


def fetch(source: str, slug: str | None = None) -> dict:
    """Return a log's recent text. source: core|supervisor|host|audio|dns|
    multicast, or 'addon' with a slug."""
    try:
        if source == "addon":
            if not slug:
                return {"error": "add-on slug required"}
            path = "/addons/%s/logs" % slug
        elif source in _SOURCES:
            path = _SOURCES[source]
        else:
            return {"error": "unknown log source '%s'" % source}
        try:
            raw = _get(path)
        except Exception as e:  # some plugins/log endpoints 400 when empty
            return {"error": "log unavailable: %s" % e, "source": source, "slug": slug}
        txt = _clean(raw.decode("utf-8", "replace"))
        truncated = len(txt) > MAX
        return {"ok": True, "source": source, "slug": slug,
                "log": txt[-MAX:], "truncated": truncated}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def targets() -> dict:
    """List installed add-ons (+ fixed sources) so the UI can offer log targets."""
    out = {"ok": True, "sources": ["core", "supervisor", "host"], "addons": []}
    try:
        raw = _get("/addons", accept="application/json")
        d = json.loads(raw.decode())
        items = (d.get("data") or {}).get("addons") or []
        out["addons"] = sorted(
            [{"slug": a.get("slug"), "name": a.get("name"),
              "state": a.get("state"), "version": a.get("version")}
             for a in items if a.get("slug")],
            key=lambda a: (a.get("name") or "").lower())
    except Exception as e:  # noqa: BLE001
        out["addons_error"] = str(e)
    return out
