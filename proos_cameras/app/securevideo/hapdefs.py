"""ProOS Cameras — the Apple Home services and characteristics Secure Video needs, made for HAP-python.
Register 970u, Route 2.

HAP-python 5.0.0 knows none of them (its definitions stop at the classic camera). `hapdefs.json` holds the 14 services
and 54 characteristics exactly as HAP-NodeJS PR #1132 defines them (UUIDs, formats, permissions, ranges), extracted
from its definitions source. This module turns them into HAP-python objects.

`Char` adds one thing to HAP-python's characteristic: a write handler that is told WHICH HAP connection wrote
(the Data Stream setup needs that connection's session secret). HAP-python's own setter callback only gets the value.
"""
from __future__ import annotations

import json
import os
import uuid

from pyhap.characteristic import HAP_FORMAT_DEFAULTS, Characteristic
from pyhap.service import Service

_DEFS = json.load(open(os.path.join(os.path.dirname(__file__), "hapdefs.json")))
SERVICES, CHARS = _DEFS["services"], _DEFS["chars"]

_PERMS = {"PAIRED_READ": "pr", "PAIRED_WRITE": "pw", "NOTIFY": "ev", "WRITE_RESPONSE": "wr", "TIMED_WRITE": "tw"}


class Char(Characteristic):
    """A characteristic whose write handler sees the writing connection: handler(value, client_addr) -> response."""

    write_handler = None

    def client_update_value(self, value, sender_client_addr=None):
        if self.write_handler is None:
            return super().client_update_value(value, sender_client_addr)
        response = self.write_handler(value, sender_client_addr)
        return response

    def to_HAP(self, include_value=True):
        """As HAP-NodeJS lists an accessory: a read that fails shows the format's default value in the listing
        (a direct read still answers with the error). HAP-python would otherwise fail the whole listing."""
        try:
            return super().to_HAP(include_value)
        except Exception:
            rep = dict(super().to_HAP(include_value=False))
            rep["value"] = HAP_FORMAT_DEFAULTS.get(self.properties["Format"], "")
            return rep


def make_char(name: str) -> Char:
    d = CHARS[name]
    props = {"Format": d["Format"], "Permissions": [_PERMS[p] for p in d["Permissions"]]}
    for k in ("minValue", "maxValue", "minStep"):
        if k in d:
            props[k] = d[k]
    if "validValues" in d and d["Format"] != "bool":     # a bool sends true/false; a 0/1 list would make it a number
        props["ValidValues"] = {str(v): v for v in d["validValues"]}
    return Char(name, uuid.UUID(d["UUID"]), props)


def make_service(name: str, optional=(), display_name=None) -> Service:
    d = SERVICES[name]
    svc = Service(uuid.UUID(d["UUID"]), display_name or name)
    for c in d["req"]:
        svc.add_characteristic(make_char(c))
    for c in optional:
        if c not in d["opt"]:
            raise KeyError("%s is not an optional characteristic of %s" % (c, name))
        svc.add_characteristic(make_char(c))
    return svc


def char(svc: Service, name: str) -> Char:
    want = uuid.UUID(CHARS[name]["UUID"])
    for c in svc.characteristics:
        if c.type_id == want:
            return c
    raise KeyError("%s has no %s" % (svc.display_name, name))


def has(svc: Service, name: str) -> bool:
    want = uuid.UUID(CHARS[name]["UUID"])
    return any(c.type_id == want for c in svc.characteristics)
