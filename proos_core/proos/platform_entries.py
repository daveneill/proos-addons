"""ProOS Core — the platform's own config entries, read where they live.

REGISTER 439 (13 Sep 2026). Dave: "all the main settings need to be a mirror
of the native platform — I'm sick of hearing that you write a patch to make
something work when the platform already owns it." Core kept its own copy
of the UniFi controller's host, username and password (and Protect's), of
ProOS Music's host and port, of the MCP server's address. A copy is right
until the day the platform's changes; that day was 13 Sep, when Core went on
calling the controller's OLD address for 86 seconds a call.

An integration's address and login are configured IN the integration and
nowhere else. This module reads them from the platform's own config-entry
store, every time they are needed, and nothing in Core keeps a copy.

The platform's WebSocket API redacts an entry's ``data`` (host, credentials),
so the store is read on disk — the same store netmap.py already reads for
the reachability map, under the add-on's homeassistant_config map. If the
store cannot be read the answer is {} and the caller says so; nothing is
guessed and nothing is remembered.
"""
from __future__ import annotations

import json
import os

_STORAGE = ("/homeassistant" if os.path.isdir("/homeassistant/.storage") else "/config") + "/.storage"
_ENTRIES = os.environ.get("PROOS_PLATFORM_ENTRIES", os.path.join(_STORAGE, "core.config_entries"))


def _all() -> list:
    try:
        with open(_ENTRIES, encoding="utf-8") as fh:
            d = json.load(fh) or {}
        return list(((d.get("data") or {}).get("entries") or []))
    except Exception:  # noqa: BLE001 — unreadable store: no entries, no guess
        return []


def entries(domain: str) -> list:
    """Every entry the platform holds for an integration domain, disabled ones
    left out, in the platform's own order."""
    return [e for e in _all() if e.get("domain") == domain and not e.get("disabled_by")]


def entry(domain: str) -> dict | None:
    """The platform's first live entry for the domain, or None."""
    es = entries(domain)
    return es[0] if es else None


def data(domain: str) -> dict:
    """The entry's own ``data`` — host, port, username, password, verify_ssl,
    api_key, url, whatever the integration keeps — or {} when there is none."""
    e = entry(domain)
    return dict((e or {}).get("data") or {})


def host_of(domain: str) -> str:
    """The host the platform itself talks to for this integration, '' if none."""
    d = data(domain)
    for k in ("host", "address", "hostname", "ip", "ip_address"):
        v = d.get(k)
        if v:
            return str(v).strip()
    url = d.get("url")
    if url:
        try:
            from urllib.parse import urlparse
            return urlparse(str(url)).hostname or ""
        except Exception:  # noqa: BLE001
            return ""
    return ""


def by_id(entry_id: str) -> dict | None:
    """The platform's entry with this id, or None."""
    for e in _all():
        if e.get("entry_id") == entry_id:
            return e
    return None


def options(entry_id: str) -> dict:
    """The entry's own ``options`` — what its options flow saved — or {}."""
    return dict((by_id(entry_id) or {}).get("options") or {})
