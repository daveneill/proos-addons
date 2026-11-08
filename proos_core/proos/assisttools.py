"""ASSIST'S PLATFORM TOOLS ARE PART OF PROOS — register 1253.

Dave, 8 Nov 2026, after "assist fix the bedroom" handed the job to the installer: "Why … was the platform tools removed
its the … main tool for the whole communication to the platform"; then "yes please" to Core keeping it.

HOW IT WAS LOST (read, register 1253): the platform's tool server — the Home Assistant MCP Server add-on
(github.com/homeassistant-ai/ha-mcp) — was installed by hand on the old house box in August. On 18 Oct the ProOS image
was built (register 942) and the home moved onto the flashed G-Flip; the image's first boot and Core's own set-up
install Core, Whisper, Piper, Matter, the microphone and the panel screen — never the platform's tool server. Nothing
noticed: Core printed one line to its log, and Assist carried on without the platform's integration details, its log
or its service calls (register 1252, measured).

WHAT THIS DOES: the same thing panelsetup does for the panel's pieces, through the platform's own machinery (the
Supervisor's store): the add-on's repository is in the store, the add-on is installed, starts on boot, and is running.
A box where all of that is already true gets no writes (converge, don't clobber). Its options are the add-on's own —
it writes its own secret address into them on first start, which is exactly where Core's discovery reads it (ha_mcp).
"""
from proos import panelsetup as _ps

REPO = "https://github.com/homeassistant-ai/ha-mcp"
ADDON = "ha_mcp"


def ensure(sup) -> dict:
    """`sup(method, path, payload=None, timeout=...)` is the Supervisor (Core's _sv). Returns what it did; `done` only
    when the add-on is installed, on boot and started."""
    out = {"done": False}
    slug = _ps._repo_slug(sup, REPO)
    if not slug:
        sup("POST", "/store/repositories", {"repository": REPO})
        slug = _ps._repo_slug(sup, REPO)
        out["repository_added"] = bool(slug)
    if not slug:
        out["error"] = "the platform's store did not take the repository %s" % REPO
        return out
    addon = "%s_%s" % (slug, ADDON)
    out["addon"] = addon
    out.update(_ps.ensure_addon(sup, addon, None, start=True))
    try:
        info = sup("GET", "/addons/%s/info" % addon) or {}
        if info.get("watchdog") is False:
            sup("POST", "/addons/%s/options" % addon, {"watchdog": True})   # the platform restarts it if it falls over
            out["watchdog"] = True
        out["done"] = bool(info.get("version")) and (sup("GET", "/addons/%s/info" % addon) or {}).get("state") == "started"
    except Exception as e:                                       # noqa: BLE001
        out["error"] = str(e)[:200]
    return out
