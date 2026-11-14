"""PROOS'S KEYS LIVE ON PROOS CONNECT — register 1347, the box's side (14 Nov 2026).

Dave 14 Nov: "I seroiusly dont know why we were using an API key for each box when we could have none and have one in
ProConnect" … "just build the whole lot including the ChatGPT option to run on ProConnect and remove it all from Pro".

The box holds no key of ProOS's own. Every call that needs one — the AI (Claude or ChatGPT), a scene picture, Talk Live's
session key, an iPhone alert, the Apple Music token — goes to ProOS Connect with the box's own Connect credential (the
id and secret it made at enrolment, connectvoice.identity()), and Connect adds the key, counts the use and hands back
the answer. One key per supplier, for every home; a home's plan is enforced there (a suspended home is refused).

THE RULE FOR A BOX THAT STILL HOLDS A KEY OF ITS OWN: Connect first, whenever Connect says it holds that key; the box's
own key only when Connect does not (not yet set up, or not reachable when the box last asked) — so nothing stops
working while the keys move. REGISTER 1348 (Dave 14 Nov 15:04): no Developer override and no key field in Pro; once
Connect says it holds a key, the box's own copy is removed (retire, below) — automatically, every time Connect is read.

How Assist's existing code is pointed here without touching its forty call sites: in Connect mode the active key Assist
reads is CONNECT — a marker, not a key — and wire() turns any provider request carrying the marker into the same request
to Connect with the box's credential. A request carrying a real key is left exactly as it was.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import connectvoice

CONNECT = "proos-connect"            # the marker: "Connect holds this key" — never a key, never sent anywhere
DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")
POLICY_PATH = os.path.join(DATA_DIR, "connect_policy.json")
POLICY_TTL_S = 600                    # plumbing: how often the box re-reads the fleet's AI setting from Connect
_HOSTS = {"https://api.anthropic.com": "anthropic", "https://api.openai.com": "openai"}
_PROV = {"anthropic": "claude", "openai": "openai"}      # Connect's supplier names → Assist's provider names
DEFAULT_POLICY = {"provider": "anthropic", "model": "claude-opus-5-5", "ready": {}}

_lock = threading.Lock()
_refreshing = {"on": False, "at": 0.0}
_music = {"token": None, "expires": 0, "failed": 0}


def _url():
    return connectvoice.CONNECT_URL


def _creds():
    st = connectvoice.identity()
    return {"X-ProOS-Box": st["box_id"], "Authorization": "Bearer " + st["secret"]}


def _get(path, timeout=8):
    req = urllib.request.Request(_url() + path, method="GET", headers=dict(_creds(), **{"User-Agent": "proos-core"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# ── the fleet's AI setting: which supplier, which model, which keys Connect holds ─────────────────────────────────────
def _read_cache():
    try:
        with open(POLICY_PATH, encoding="utf-8") as fh:
            d = json.load(fh) or {}
        return d if isinstance(d, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


RETIRE = []      # register 1348: callables(ready) the server adds — each removes the box's own copy of a key Connect holds


def refresh(fetch=None):
    """Ask Connect now. Kept on the box so a question never waits on it. Returns the policy, or None. Then every box
    copy of a key Connect holds is removed (RETIRE)."""
    try:
        pol = (fetch or (lambda: _get("/v1/ai/policy")))()
    except Exception as e:                                       # noqa: BLE001
        print("  [connect] the AI setting could not be read from ProOS Connect: %s" % str(e)[:120], flush=True)
        return None
    if not isinstance(pol, dict):
        return None
    pol = dict(pol, read_at=int(time.time()))
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = POLICY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(pol, fh)
        os.replace(tmp, POLICY_PATH)
    except Exception:                                            # noqa: BLE001
        pass
    for fn in list(RETIRE):
        try:
            gone = fn(pol.get("ready") or {}) or []
            if gone:
                print("  [connect] ProOS Connect holds them now — removed from this box: %s" % ", ".join(gone), flush=True)
        except Exception as e:                                   # noqa: BLE001
            print("  [connect] couldn't remove a box key Connect now holds: %s" % str(e)[:120], flush=True)
    return pol


def policy():
    """What Connect last said — at once, from the box. A stale copy is refreshed in the background."""
    pol = _read_cache()
    now = time.time()
    # asked only by a box that has a Connect identity, and at most once per POLICY_TTL_S whether or not it answered
    if (now - float(pol.get("read_at") or 0) > POLICY_TTL_S and now - _refreshing.get("at", 0) > POLICY_TTL_S
            and os.path.exists(connectvoice.STATE_PATH)):
        with _lock:
            go = not _refreshing["on"]
            _refreshing["on"] = True
            _refreshing["at"] = now
        if go:
            def _run():
                try:
                    refresh()
                finally:
                    _refreshing["on"] = False
            threading.Thread(target=_run, daemon=True).start()
    return pol


def holds(what, pol=None):
    """Does Connect hold this key ('anthropic', 'openai', 'push', 'music')? Only what Connect itself said."""
    return bool(((pol if pol is not None else policy()).get("ready") or {}).get(what))


def ai_choice(pol=None):
    """(Assist provider, model) as the fleet setting says, or (None, None) when Connect has said nothing yet."""
    pol = pol if pol is not None else policy()
    prov = _PROV.get(pol.get("provider") or "")
    if not prov:
        return None, None
    return prov, (pol.get("model") or "")


# ── the wire: a provider request carrying the marker goes to Connect instead ──────────────────────────────────────────
def wire(url, headers):
    """(url, headers) for one provider request. Carrying the marker → the same request to ProOS Connect with this box's
    credential, the provider's own non-key headers kept. Carrying a real key (or anything else) → unchanged."""
    headers = dict(headers or {})
    marked = (headers.get("x-api-key") == CONNECT
              or headers.get("Authorization") == "Bearer " + CONNECT)
    if not marked:
        return url, headers
    for host, prov in _HOSTS.items():
        if url.startswith(host + "/"):
            out = {k: v for k, v in headers.items() if k.lower() not in ("x-api-key", "authorization")}
            out.update(_creds())
            return _url() + "/v1/ai/" + prov + url[len(host):], out
    raise RuntimeError("ProOS Connect does not carry that request (%s)" % url.split("?")[0][:80])


# ── Apple Music: the developer token, made on Connect ─────────────────────────────────────────────────────────────────
def music_token(fetch=None, now=None):
    """The Apple Music developer token Connect signs with ProOS's MusicKit key — kept until shortly before it ends."""
    now = now or time.time()
    if _music["token"] and _music["expires"] - 300 > now:
        return _music["token"]
    if now - _music.get("failed", 0) < 60:              # plumbing: a refused or unreachable ask waits a minute
        return None
    try:
        d = (fetch or (lambda: _get("/v1/music/token")))()
    except Exception as e:                                       # noqa: BLE001
        print("  [connect] no Apple Music token from ProOS Connect: %s" % str(e)[:120], flush=True)
        _music["failed"] = now
        return None
    if not (d or {}).get("token"):
        _music["failed"] = now
        return None
    _music.update(token=d["token"], expires=float(d.get("expires") or now + 3600))
    return _music["token"]


# ── iPhone alerts: signed and sent by Connect ────────────────────────────────────────────────────────────────────────
def push(token, payload: bytes, topic, sandbox, post=None):
    """Send one alert through Connect. Returns push.send's own result shape."""
    body = {"token": token, "topic": topic, "sandbox": bool(sandbox),
            "payload": json.loads(payload.decode("utf-8")) if isinstance(payload, bytes) else payload}

    def _post():
        req = urllib.request.Request(_url() + "/v1/push", method="POST", data=json.dumps(body).encode(),
                                     headers=dict(_creds(), **{"Content-Type": "application/json",
                                                               "User-Agent": "proos-core"}))
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}")
            except Exception:                                    # noqa: BLE001
                return {"ok": False, "error": "ProOS Connect HTTP %s" % e.code}
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "error": "ProOS Connect could not be reached: %s" % str(e)[:120]}
    d = (post or _post)() or {}
    if d.get("ok"):
        return {"ok": True, "status": 200, "via": "connect"}
    reason = ""
    try:
        reason = (json.loads(d.get("said") or "{}") or {}).get("reason", "")
    except Exception:                                            # noqa: BLE001
        reason = (d.get("said") or "")[:120]
    status = int(d.get("apple") or 0)
    return {"ok": False, "status": status, "reason": reason or d.get("error") or "not sent", "via": "connect",
            "unregistered": status == 410 or reason == "Unregistered"}


# ── what Pro shows: this home's use this month ───────────────────────────────────────────────────────────────────────
def usage(fetch=None):
    try:
        return dict((fetch or (lambda: _get("/v1/usage")))(), ok=True)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "error": "ProOS Connect could not be reached: %s" % str(e)[:120]}
