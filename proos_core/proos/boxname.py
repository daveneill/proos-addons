"""THE BOX'S NETWORK NAME — register 826 (Dave 13 Oct 14:11 "proos-a3bb2e66 using boxes id works", 14:13 "go").

READ (825): the name is set the platform's way — the Supervisor's POST /host/options {hostname} — the same call provisioning makes on a
factory first boot (provision._set_hostname). A shipped box is named proos-<first 8 of its site id> there; a box set up any other way
(Dave's development box: "homeassistant") is renamed here, deliberately, by a developer. The box's own proos-<id> is offered; any
valid network name is accepted (letters, digits and hyphens, up to 63, not starting or ending with a hyphen). After the call the
Supervisor is asked again — the name is only reported as changed when the box itself says so.
"""
from __future__ import annotations

import re

from proos import provision

NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def suggested():
    """This box's own proos-<id>: the name provisioning chose (or would have), from its site id."""
    flag = provision._load_flag() or {}
    if flag.get("hostname"):
        return flag["hostname"]
    sid = flag.get("site_id") or ""
    return ("%s-%s" % (provision.HOSTNAME_PREFIX, sid[:8])) if sid else None


def status(info_fn):
    host = info_fn("/host/info") or {}
    return {"name": host.get("hostname"), "suggested": suggested()}


def valid(name):
    return bool(NAME.match(name or ""))


def rename(name, info_fn, req_fn):
    """Rename the box. Returns {ok, words, name}."""
    name = (name or "").strip().lower()
    if not valid(name):
        return {"ok": False, "words": "A box name is letters, numbers and hyphens (up to 63), and can't start or end with a hyphen."}
    before = (info_fn("/host/info") or {}).get("hostname")
    if before == name:
        return {"ok": False, "words": "The box is already called %s." % name, "name": name}
    try:
        req_fn("POST", "/host/options", {"hostname": name})
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "words": "The system manager refused it: %s" % str(e)[:200], "name": before}
    after = (info_fn("/host/info") or {}).get("hostname")
    if after != name:
        return {"ok": False, "words": "The system manager took the request, but the box still reports %s." % (after or "no name"),
                "name": after}
    return {"ok": True, "words": "The box is now %s on the network (it was %s). Anything that reached it as %s.local needs the new name."
            % (name, before, before), "name": name, "was": before}
