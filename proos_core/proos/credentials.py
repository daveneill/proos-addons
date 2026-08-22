"""Centralised credential / token store for ProOS Core.

Generalises the per-feature token files (``ma_conn.json``, ``ma_admin.json``, ...)
into one registry so services -- Music Assistant today, other integrations
tomorrow -- can mint, store, rotate and revoke API tokens through a common
interface instead of each feature growing its own bespoke ``/data/*.json``.

Security boundary (matches the rest of Core):
  * Tokens live here, server-side, under the add-on's private ``/data`` volume.
  * ``get()`` returns the raw token and is for SERVER-SIDE callers only.
  * ``status()`` is masked (name / kind / host / created / rotated / last4) and
    is the ONLY shape that should ever be handed to a browser. The raw secret
    never leaves the box.

The store is deliberately dependency-free and service-agnostic. Per-service
"how do I mint / validate a token" logic stays with that service (e.g. ma.py);
this module only owns storage, masking and the token lifecycle bookkeeping.
"""
from __future__ import annotations

import json
import os
import threading
import time


def _now() -> int:
    return int(time.time())


def _last4(tok: str) -> str | None:
    if not tok:
        return None
    return tok[-4:] if len(tok) >= 4 else "*" * len(tok)


class CredentialStore:
    """A JSON-backed, service-keyed token registry.

    File shape::

        {"services": {"<service>": {"token": "...", "meta": {...}}}}
    """

    def __init__(self, path: str = "/data/credentials.json"):
        self._path = path
        self._lock = threading.Lock()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                d = json.load(f) or {}
        except Exception:
            d = {}
        if not isinstance(d.get("services"), dict):
            d["services"] = {}
        return d

    def _save(self, d: dict) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self._path)  # atomic swap; never leaves a half-written file

    # ── write ────────────────────────────────────────────────────────────
    def put(self, service: str, token: str, *, name: str | None = None,
            host: str | None = None, port=None, kind: str = "long_lived",
            extra: dict | None = None) -> dict:
        """Store (or replace) a service's token. Returns the masked status.

        If a different token was already present, the change is recorded as a
        rotation (``meta.rotated``). ``meta.created`` is preserved across writes.
        """
        with self._lock:
            d = self._load()
            svcs = d["services"]
            prev = svcs.get(service) or {}
            meta = dict(prev.get("meta") or {})
            now = _now()
            if prev.get("token") and prev.get("token") != token:
                meta["rotated"] = now
            meta.setdefault("created", now)
            if name is not None:
                meta["name"] = name
            if host is not None:
                meta["host"] = host
            if port is not None:
                meta["port"] = port
            meta["kind"] = kind
            if extra:
                meta.update(extra)
            meta["last4"] = _last4(token)
            svcs[service] = {"token": token, "meta": meta}
            self._save(d)
            return self._mask(service, svcs[service])

    def rotate(self, service: str, new_token: str) -> dict | None:
        """Swap an existing token for a freshly minted one. ``None`` if unknown."""
        with self._lock:
            cur = self._load()["services"].get(service)
        if not cur:
            return None
        m = cur.get("meta") or {}
        return self.put(service, new_token, name=m.get("name"),
                        host=m.get("host"), port=m.get("port"),
                        kind=m.get("kind", "long_lived"))

    def delete(self, service: str) -> bool:
        """Revoke locally: drop the stored token. (Revoking it at the service's
        end, where supported, is the caller's job before/after this.)"""
        with self._lock:
            d = self._load()
            if service in d["services"]:
                del d["services"][service]
                self._save(d)
                return True
        return False

    # ── read ─────────────────────────────────────────────────────────────
    def get(self, service: str) -> str | None:
        """Raw token -- SERVER-SIDE ONLY. Never send this to a client."""
        return (self._load()["services"].get(service) or {}).get("token")

    def meta(self, service: str) -> dict:
        return dict((self._load()["services"].get(service) or {}).get("meta") or {})

    def _mask(self, service: str, entry: dict) -> dict:
        m = dict(entry.get("meta") or {})
        return {
            "service": service,
            "set": bool(entry.get("token")),
            "name": m.get("name"),
            "kind": m.get("kind"),
            "host": m.get("host"),
            "port": m.get("port"),
            "created": m.get("created"),
            "rotated": m.get("rotated"),
            "last4": m.get("last4"),
        }

    def status(self, service: str | None = None):
        """Masked status. One dict for a named service, else a sorted list."""
        d = self._load()
        if service is not None:
            e = d["services"].get(service)
            return self._mask(service, e) if e else {"service": service, "set": False}
        return [self._mask(s, e) for s, e in sorted(d["services"].items())]
