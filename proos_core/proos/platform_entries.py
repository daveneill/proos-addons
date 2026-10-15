"""ProOS Core — the platform's own config entries, read where they live.

REGISTER 439 (13 Sep 2026). Dave: "all the main settings need to be a mirror
of the native platform — I'm sick of hearing that you write a patch to make
something work when the platform already owns it." Core kept its own copy
of the UniFi controller's host, username and password (and Protect's), of
ProOS Music's host and port, of the MCP server's address. A copy is right
until the day the platform's changes; that day was 13 Sep, when Core went on
calling the controller's OLD address for 86 seconds a call.

An integration's address and login are configured IN the integration and
nowhere else. This module reads the address from the platform's own
config-entry store, every time it is needed, and nothing in Core keeps a copy.
Register 695: the login is never read at all — see scrub().

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


# REGISTER 695 (Dave, 6 Oct 2026, "yes"): ProOS reads an integration's ADDRESS,
# never its saved login. Passwords, usernames and tokens are dropped the moment
# the store is read, so no caller can ever hold one. The one key kept is
# UniFi Protect's own `api_key`: Dave ruled the same day that ProOS's direct
# Protect features stay (talk-back, PTZ, snapshots), and they need that key.
_SECRET_KEYS = frozenset(("password", "username", "user", "token", "access_token",
                          "refresh_token", "client_secret", "secret", "pin", "psk",
                          "passphrase", "passkey"))
_SECRET_PARTS = ("password", "secret", "token", "passphrase")


def _is_secret(key) -> bool:
    k = str(key).lower()
    if k == "api_key":
        return False
    return k in _SECRET_KEYS or any(part in k for part in _SECRET_PARTS)


def scrub(entry: dict) -> dict:
    """A copy of a config entry with every login, password and token removed
    from its ``data`` and ``options`` — the address and the settings stay."""
    out = dict(entry or {})
    for part in ("data", "options"):
        if isinstance(out.get(part), dict):
            out[part] = {k: v for k, v in out[part].items() if not _is_secret(k)}
    return out


def _all() -> list:
    try:
        with open(_ENTRIES, encoding="utf-8") as fh:
            d = json.load(fh) or {}
        return [scrub(e) for e in ((d.get("data") or {}).get("entries") or [])]
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
    """The entry's own ``data`` — host, port, verify_ssl, url, whatever the
    integration keeps except its login (register 695) — or {} when there is none."""
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
