"""AN INTEGRATION STUCK AFTER A POWER CUT IS RELOADED ONCE ITS DEVICE ANSWERS — register 798.

Dave, 12 Oct 2026 18:03: "we had power off a while ago and it should just reconnect when back"; 18:06 "go on both".
READ ON THE BOX (796/797): after the power returned, the box came up before the UniFi console. While the console
could not be reached the platform retried by itself (its "not ready" road). When the console answered but was still
starting — its site not yet listed — the integration's own code raised an error the platform does NOT retry (the same
line in the custom copy and the platform's built-in), and the integration stayed in "setup_error" until a person
reloaded it. Every Sonos room lost its address with it.

THIS IS THE RECOVERY LAYER (Rule Zero: recovery and consent may hold rules — Dave said yes, 18:06). The rule, in full:
an integration entry the platform reports as "setup_error" is reloaded through the platform's own reload — ONLY when
the device its entry talks to answers (a TCP connection to its own address and port opens — measured, not assumed),
at most once every RETRY_GAP_S, and at most MAX_TRIES times until the platform reports that entry in any other state.
Each try is written to the Master Log with what was measured. Runs only with the add-on's auto_heal on.
An entry with no address the platform holds is left alone (nothing to ask) — said once in Core's log.
"""
from __future__ import annotations

import socket
import time
from urllib.parse import urlparse

RETRY_GAP_S = 300        # plumbing: the least time between two reloads of one entry
MAX_TRIES = 3            # plumbing: reloads per stuck spell, before a person has to look (Health still shows it)
PROBE_TIMEOUT_S = 3      # plumbing: how long one "does the device answer" connection may take
DEFAULT_PORTS = (443, 80)  # plumbing: where to knock when the entry names no port (https first, then http)

_TRIED = {}              # entry_id -> [tries this spell, last try time]
_SAID_NO_HOST = set()


def target(entry_data):
    """(host, [ports]) the entry's own data says it talks to, or (None, []) — read, never guessed."""
    d = entry_data or {}
    host, port = None, None
    url = d.get("url")
    if url:
        try:
            u = urlparse(str(url))
            host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80 if u.scheme == "http" else None)
        except Exception:                                        # noqa: BLE001
            host = None
    for k in ("host", "address", "hostname", "ip", "ip_address"):
        if not host and d.get(k):
            host = str(d.get(k)).strip()
    if not host:
        return None, []
    try:
        if d.get("port"):
            port = int(d.get("port"))
    except (TypeError, ValueError):
        pass
    return host, ([port] if port else list(DEFAULT_PORTS))


def answers(host, ports, connect=None):
    """True when a TCP connection to the device opens on one of its ports (the device is up and listening)."""
    connect = connect or socket.create_connection
    for p in ports:
        try:
            s = connect((host, int(p)), PROBE_TIMEOUT_S)
            try:
                s.close()
            except Exception:                                    # noqa: BLE001
                pass
            return True
        except Exception:                                        # noqa: BLE001
            continue
    return False


def check(entries, entry_data, reload_fn, log, now=None, connect=None):
    """One look. entries: the platform's config entries (state per entry). entry_data(entry_id) → that entry's own
    data (the address). reload_fn(entry_id) → the platform's own reload. log(event dict) → the Master Log.
    Returns the list of what was done or left, for the caller and the benches."""
    now = now or time.time()
    done = []
    stuck_now = set()
    for e in entries or []:
        eid, dom, st = e.get("entry_id"), e.get("domain"), e.get("state")
        if not eid:
            continue
        if st != "setup_error" or e.get("disabled_by"):
            _TRIED.pop(eid, None)                                # any other state ends the spell
            continue
        stuck_now.add(eid)
        tries, last = _TRIED.get(eid, [0, 0.0])
        if tries >= MAX_TRIES:
            done.append({"entry": eid, "domain": dom, "did": "gave_up", "tries": tries})
            continue
        if now - last < RETRY_GAP_S:
            continue
        host, ports = target(entry_data(eid))
        if not host:
            if eid not in _SAID_NO_HOST:
                _SAID_NO_HOST.add(eid)
                print("  [reconnect] %s (%s) is stuck and its entry holds no address to ask — left for a person"
                      % (e.get("title") or dom, dom), flush=True)
            done.append({"entry": eid, "domain": dom, "did": "no_address"})
            continue
        if not answers(host, ports, connect):
            done.append({"entry": eid, "domain": dom, "did": "device_silent", "host": host})
            continue
        _TRIED[eid] = [tries + 1, now]
        ok, why = True, None
        try:
            reload_fn(eid)
        except Exception as ex:                                  # noqa: BLE001
            ok, why = False, str(ex)[:160]
        ev = {"action": "reload_integration", "why": "stuck in setup_error; its device answers again",
              "integration": dom, "title": e.get("title"), "host": host, "try": tries + 1, "ok": ok}
        if why:
            ev["error"] = why
        log(ev)
        print("  [reconnect] %s (%s) was stuck after its device came back — %s answers; reloaded (try %d)%s"
              % (e.get("title") or dom, dom, host, tries + 1, "" if ok else " — the reload failed: %s" % why), flush=True)
        done.append({"entry": eid, "domain": dom, "did": "reloaded", "host": host, "try": tries + 1, "ok": ok})
    for eid in list(_TRIED):
        if eid not in stuck_now:
            _TRIED.pop(eid, None)
    return done
