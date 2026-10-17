"""WHERE THE PLATFORM IS, ASKED — NEVER ASSUMED (register 929, 17 Oct 2026).

Core reaches the platform directly, by the Supervisor's own name for it
(``homeassistant``), wherever the Supervisor's proxy cannot serve: checking a
person's own sign-in (auth.verify — the proxy swaps in its own token), the
installer door (proauth) and onboarding (provision). That address carried the
port 8123 written in the source. Home Assistant 2026.8 made port 80 the
default for NEW Home Assistant OS installs (existing installs keep 8123), so
on every new box — the G-Flip master first — Core rejected every sign-in:
"authentication required" to the owner's own valid token.

The platform's own answer: the Supervisor's documented ``GET /core/info``
returns ``port`` ("The port Home Assistant is running on") and ``ssl`` to an
add-on with the Supervisor API (Core has it: hassio_api). This module asks it,
keeps the answer, asks again after PLATFORM_ADDR_RECHECK_S, and falls back to
8123 — the old address — only when the Supervisor cannot be asked at all
(Core off-box on a bench, or the Supervisor restarting). A port that changes
needs the platform to restart, so a few minutes' memory is ample.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

_LOG = logging.getLogger("proos.platformaddr")

HOST = "homeassistant"                 # the Supervisor's own name for the platform
FALLBACK_PORT = 8123                   # only when the Supervisor cannot be asked
PLATFORM_ADDR_RECHECK_S = 300          # PLUMBING: how long one answer is kept

_cache = {"base": None, "at": 0.0, "source": None}


def _ask_supervisor(timeout: float = 5.0) -> dict:
    """The Supervisor's /core/info `data` block. Raises when it cannot be asked."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("no Supervisor token (not running as an add-on)")
    req = urllib.request.Request("http://supervisor/core/info",
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 — the Supervisor
        body = json.loads(r.read().decode("utf-8") or "{}")
    return body.get("data") or body


# Swappable for benches: a function returning /core/info's data, or raising.
ASK = _ask_supervisor


def base(force: bool = False) -> str:
    """The platform's own address from inside the box, e.g.
    'http://homeassistant:80' — always with an explicit port (a URL without
    one would be read as 8123 by ha_ws._ws_target)."""
    now = time.time()
    if not force and _cache["base"] and now - _cache["at"] < PLATFORM_ADDR_RECHECK_S:
        return _cache["base"]
    try:
        info = ASK() or {}
        port = int(info.get("port") or 0)
        if port <= 0:
            raise ValueError("no port in the Supervisor's answer")
        scheme = "https" if info.get("ssl") else "http"
        out, src = "%s://%s:%d" % (scheme, HOST, port), "supervisor"
    except Exception as e:  # noqa: BLE001 — keep the last word, else the old address
        if _cache["base"] and _cache["source"] == "supervisor":
            _LOG.debug("platform address: Supervisor not answering (%s) — keeping %s", e, _cache["base"])
            _cache["at"] = now
            return _cache["base"]
        out, src = "http://%s:%d" % (HOST, FALLBACK_PORT), "fallback"
        _LOG.info("platform address: Supervisor not answering (%s) — using %s", e, out)
    if out != _cache["base"]:
        _LOG.info("platform address: %s (from the %s)", out, src)
    _cache.update(base=out, at=now, source=src)
    return out


def source() -> str | None:
    """'supervisor' when the address came from the platform's own answer,
    'fallback' when it is the old assumed one, None before the first ask."""
    return _cache["source"]
