"""THE UNIFI CARD IS THE ONE SPOT — register 1255.
Run: python3 tests/the_unifi_card_is_the_one_spot_bench.py

Dave, 8 Nov 2026: "it needs to be all done from one spot this cant be messed up by having multiple location"; 16:28
"yes but this needs to still support homes that might have only one or none of these integrations". MEASURED: the
platform's UniFi Network connection lost its site on 6 Nov 17:42 when the console's users were changed for the suite,
565 entities unavailable; the card borrowed its address from that broken connection; the Health card's "since" was the
last read, not when the fault began.

PINNED (driven — the real unilink.ensure / state, the real uniconsole.host / adopt_host / signin over a real
CredentialStore, the real platformfaults.refresh / cards; the platform and the console are stand-ins):
 1. a home with no card sign-in: nothing written
 2. a console without UniFi Network: nothing written, said so
 3. card + Network, no connection: the platform's own form filled from the card (address, local user, its password),
    set up — and the answer carries no password
 4. a running connection: NO writes (converge)
 5. the platform's own sign-in request is answered from the card
 6. a broken connection is NOT removed without the installer's press — said, no writes
 7. the press: removed, then set up again from the card
 8. a console with several sites: stops, the half-made set-up is dropped, nothing created
 9. the card owns its address: its own first; a card signed in before 1255 adopts the platform's address once
10. a platform card's "since" is when the fault was first seen (kept across reads); a UniFi card offers Set Up Again
11. (SOURCE) the keeper runs on every box, the module ships, Pro offers the button
12. (1256) UniFi Protect the same way: its own platform form, given ProOS's API key only because that form asks for one;
    a Protect-only console sets up Protect and not Network
13. (906, kept) a console the platform has already FOUND: its own found set-up is carried on, not a second one started
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_unilink_")

from proos import credentials as C                             # noqa: E402
from proos import uniconsole as U                              # noqa: E402
from proos import unilink as L                                 # noqa: E402
from proos import platformfaults as PF                         # noqa: E402

PASS, FAIL = 0, []
PW = "s3cret-Pa55"


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "\n        wanted %r\n        got    %r" % (want, got))


def store(signed=True, host="192.168.8.1"):
    s = C.CredentialStore(tempfile.mktemp(suffix=".json"))
    if signed:
        s.put(U.SVC_LOGIN, PW, name="UniFi console local user", host=host, port=443, kind="login", extra={"user": "proos"})
    return s


class Platform:
    def __init__(self, entries=(), sites=1, reauth=False, refuse=False, found=None):
        self.entries = [dict(e) for e in entries]
        self.sites, self.refuse, self.writes, self.bodies = sites, refuse, [], []
        self.flows = [{"flow_id": "r1", "handler": "unifi", "context": {"source": "reauth"}}] if reauth else []
        if found:
            self.flows.append({"flow_id": "d1", "handler": found, "context": {"source": "integration_discovery"}})

    def ws(self, t, **kw):
        if t == "config_entries/get":
            return [e for e in self.entries if e["domain"] == kw.get("domain")]
        if t == "config_entries/flow/progress":
            return list(self.flows)
        return []

    def req(self, method, path, body=None):
        self.writes.append("%s %s" % (method, path))
        if body is not None:
            self.bodies.append(body)
        if method == "GET" and path == L.FLOW + "/r1":
            return {"type": "form", "step_id": "reauth_confirm", "flow_id": "r1",
                    "data_schema": [{"name": n} for n in ("host", "username", "password", "port", "verify_ssl")]}
        if method == "GET" and path == L.FLOW + "/d1":
            return {"type": "form", "step_id": "discovery_confirm", "flow_id": "d1",
                    "data_schema": [{"name": n} for n in ("username", "password", "api_key")]}
        if method == "POST" and path == L.FLOW + "/d1":
            self.entries.append({"entry_id": "newp", "domain": "unifiprotect", "state": "loaded"})
            return {"type": "create_entry", "result": {"entry_id": "newp"}}
        if method == "POST" and path == L.FLOW:
            if body and body.get("handler") == "unifiprotect":
                return {"type": "form", "step_id": "user", "flow_id": "p1",
                        "data_schema": [{"name": n} for n in ("host", "port", "verify_ssl", "username", "password", "api_key")]}
            return {"type": "form", "step_id": "user", "flow_id": "f1",
                    "data_schema": [{"name": n} for n in ("host", "username", "password", "port", "verify_ssl")]}
        if method == "POST" and path == L.FLOW + "/p1":
            self.entries.append({"entry_id": "newp", "domain": "unifiprotect", "state": "loaded"})
            return {"type": "create_entry", "result": {"entry_id": "newp"}}
        if method == "POST" and path == L.FLOW + "/r1":
            return {"type": "abort", "reason": "reauth_successful"}
        if method == "POST" and path == L.FLOW + "/f1":
            if self.refuse:
                return {"type": "form", "step_id": "user", "flow_id": "f1", "errors": {"base": "faulty_credentials"}}
            if self.sites > 1:
                return {"type": "form", "step_id": "site", "flow_id": "f1",
                        "data_schema": [{"name": "site", "options": [["a", "A"], ["b", "B"]][: self.sites]}]}
            self.entries.append({"entry_id": "new1", "domain": "unifi", "state": "loaded"})
            return {"type": "create_entry", "result": {"entry_id": "new1"}, "title": "Default"}
        if method == "DELETE" and path.startswith("/api/config/config_entries/entry/"):
            eid = path.rsplit("/", 1)[1]
            self.entries = [e for e in self.entries if e["entry_id"] != eid]
            return {}
        return {}


NET = [{"app": "network", "found": True}, {"app": "protect", "found": True}]
NONET = [{"app": "network", "found": False}, {"app": "protect", "found": True}]

# 1
p = Platform()
r = L.ensure(store(signed=False), p.req, p.ws, NET)
check("1 · no card sign-in: nothing written", (bool(r.get("skipped")), p.writes), (True, []))
# 2
p = Platform()
r = L.ensure(store(), p.req, p.ws, NONET)
check("2 · a console without UniFi Network: nothing written, said so",
      (r.get("done"), "does not run UniFi Network" in r.get("skipped", ""), p.writes), (True, True, []))
# 3
p = Platform()
r = L.ensure(store(), p.req, p.ws, NET)
check("3 · no connection: set up from the card", (r.get("ok"), r.get("entry_id"), p.writes[:2]),
      (True, "new1", ["POST " + L.FLOW, "POST " + L.FLOW + "/f1"]))
check("3 · the platform's form got the card's address, local user and password",
      p.bodies[-1], {"host": "192.168.8.1", "username": "proos", "password": PW, "port": 443, "verify_ssl": False})
check("3 · the answer carries no password", PW in json.dumps(r), False)
# 4
p = Platform(entries=[{"entry_id": "e1", "domain": "unifi", "state": "loaded"}])
r = L.ensure(store(), p.req, p.ws, NET)
check("4 · running: NO writes", (r.get("done"), p.writes), (True, []))
# 5
p = Platform(entries=[{"entry_id": "e1", "domain": "unifi", "state": "setup_error"}], reauth=True)
r = L.ensure(store(), p.req, p.ws, NET)
# RE-PINNED 1256: a request from the platform's in-progress list is READ first (its form decides which fields go in)
check("5 · the platform's sign-in request answered from the card (its form read first)",
      (r.get("ok"), p.writes, p.bodies[-1].get("username"), p.bodies[-1].get("password") == PW),
      (True, ["GET " + L.FLOW + "/r1", "POST " + L.FLOW + "/r1"], "proos", True))
# 6
p = Platform(entries=[{"entry_id": "e1", "domain": "unifi", "state": "setup_error"}])
r = L.ensure(store(), p.req, p.ws, NET)
check("6 · broken, no press: NOT removed, said, no writes", (r.get("needs_replace"), p.writes), ("e1", []))
# 7
p = Platform(entries=[{"entry_id": "e1", "domain": "unifi", "state": "setup_error"}])
r = L.ensure(store(), p.req, p.ws, NET, replace=True)
check("7 · the press: removed, then set up again from the card",
      (r.get("ok"), r.get("removed"), p.writes[0], [e["entry_id"] for e in p.entries]),
      (True, ["e1"], "DELETE /api/config/config_entries/entry/e1", ["new1"]))
# 8
p = Platform(sites=2)
r = L.ensure(store(), p.req, p.ws, NET)
check("8 · several sites: stops, set-up dropped, nothing created",
      (r.get("ok"), r.get("reason"), p.writes[-1], p.entries), (False, "several_sites", "DELETE " + L.FLOW + "/f1", []))
p = Platform(refuse=True)
r = L.ensure(store(), p.req, p.ws, NET)
check("8 · a refused sign-in is said in the platform's words", ("faulty credentials" in r.get("words", ""), r.get("ok")), (True, False))
# 9
s = store(host="10.0.0.9")
check("9 · the card's own address first", U.host(s, "192.168.8.1"), ("10.0.0.9", "the UniFi card in Pro"))
s = C.CredentialStore(tempfile.mktemp(suffix=".json"))
s.put(U.SVC_LOGIN, PW, name="UniFi console local user", host=None, port=443, kind="login", extra={"user": "proos"})
check("9 · a card from before 1255 borrows, then adopts the address once",
      (U.host(s, "192.168.8.1")[1], U.adopt_host(s, "192.168.8.1"), U.host(s, None), U.adopt_host(s, "1.2.3.4"),
       s.get(U.SVC_LOGIN) == PW, (s.meta(U.SVC_LOGIN) or {}).get("user")),
      ("your UniFi integration", True, ("192.168.8.1", "the UniFi card in Pro"), False, True, "proos"))
U.HTTP = lambda m, u, b=None, h=None, timeout=None: (200, {"set-cookie": "TOKEN=x; Path=/", "x-csrf-token": "c"}, b"{}")
s = C.CredentialStore(tempfile.mktemp(suffix=".json"))
U.signin(s, "", "proos", PW, platform_host="192.168.8.1")
check("9 · a sign-in keeps the address on the card", (s.meta(U.SVC_LOGIN) or {}).get("host"), "192.168.8.1")
# state
p = Platform(entries=[{"entry_id": "e1", "domain": "unifi", "state": "setup_error"}])
st = L.state(store(), p.ws, NET)
check("state · the card says the platform's connection failed to start", (st.get("ok"), "failed to start" in st.get("words", "")), (False, True))
st = L.state(store(), Platform().ws, NONET)
check("state · a console without Network is not a fault", (st.get("ok"), st.get("words")), (None, "this console does not run UniFi Network"))
# 1426, read on Dave's box 20 Nov: an IGNORED discovery (UCTRL-70A7, unifi_access) is the platform's "don't ask again" —
# beside a running connection it is never the connection the card reports
p = Platform(entries=[{"entry_id": "ig", "domain": "unifi", "state": "not_loaded", "source": "ignore"},
                      {"entry_id": "e1", "domain": "unifi", "state": "loaded"}])
st = L.state(store(), p.ws, NET)
check("1426 · an ignored discovery is never the connection — the running one is", (st.get("entry"), st.get("ok")), ("e1", True))
p = Platform(entries=[{"entry_id": "ig", "domain": "unifi", "state": "not_loaded", "source": "ignore"}])
st = L.state(store(), p.ws, NET)
check("1426 · with only an ignored discovery the platform has no connection yet — not 'is not running'",
      (st.get("entry"), "is not running" in st.get("words", "")), (None, False))


# 10 · platformfaults
class R:
    def __init__(self, ents):
        self.ents = ents

    def entries(self):
        return self.ents

    def repairs(self):
        return []

    def resolution(self):
        return {}


bad = [{"entry_id": "u1", "domain": "unifi", "title": "Default", "state": "setup_error"},
       {"entry_id": "l1", "domain": "lifx", "title": "Lamp", "state": "setup_retry"}]
PF.refresh(R(bad), now=1000.0)
PF.refresh(R(bad), now=5000.0)
cards = {c["subject"]: c for c in PF.cards()}
check("10 · since = first seen, last_seen = last read", (cards["entry:u1"]["since"], cards["entry:u1"]["last_seen"]), (1000.0, 5000.0))
check("10 · the UniFi card offers Set Up Again; another integration does not",
      ("uni_relink" in [a["kind"] for a in cards["entry:u1"]["actions"]], "uni_relink" in [a["kind"] for a in cards["entry:l1"]["actions"]]),
      (True, False))
PF.refresh(R([dict(bad[0], state="loaded"), bad[1]]), now=6000.0)
PF.refresh(R(bad), now=7000.0)
check("10 · running again clears it; failing again starts a new since",
      next(c for c in PF.cards() if c["subject"] == "entry:u1")["since"], 7000.0)

# 11 · source
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
zl = open(os.path.join(HERE, "..", "zipfiles.txt"), encoding="utf-8").read().split()
pro = open(os.path.join(HERE, "..", "apps", "pro.html"), encoding="utf-8", errors="ignore").read()
check("11 · keeper on every box (never replacing), the press replaces, the module ships, Pro has the button",
      all(x in src for x in ["_unifi_link(replace=False)", 'threading.Thread(target=_unifi_keeper, name="unifi-link"',
                             'if act["kind"] == "uni_relink":', "_unifi_link(replace=bool(b.get(\"replace\")), app=_app)"])
      and "proos/unilink.py" in zl and "'uni_relink'" in pro and "function uniRelink(" in pro, True)

# 12 · Protect
def store_key():
    s = store()
    s.put(U.SVC_KEY, "KEY-abc123", name="ProOS API key", kind="api_key")
    return s


p = Platform()
r = L.ensure(store_key(), p.req, p.ws, NET, app="protect")
check("12 · Protect: its own platform form, filled from the card with ProOS's key (its form asks for one)",
      (r.get("ok"), p.writes[:2], sorted(p.bodies[-1]), p.bodies[-1].get("api_key")),
      (True, ["POST " + L.FLOW, "POST " + L.FLOW + "/p1"], ["api_key", "host", "password", "port", "username", "verify_ssl"], "KEY-abc123"))
check("12 · …and the answer carries neither the password nor the key", ("KEY-abc123" in json.dumps(r) or PW in json.dumps(r)), False)
p = Platform()
L.ensure(store_key(), p.req, p.ws, NET)
check("12 · Network's form is NOT given the key (it does not ask for one)", "api_key" in p.bodies[-1], False)
PONLY = [{"app": "network", "found": False}, {"app": "protect", "found": True}]
p = Platform()
rn = L.ensure(store_key(), p.req, p.ws, PONLY, app="network")
rp = L.ensure(store_key(), p.req, p.ws, PONLY, app="protect")
check("12 · a Protect-only console: Network is not set up, Protect is",
      (rn.get("skipped", "").startswith("this console does not run UniFi Network"), rp.get("ok"), [e["domain"] for e in p.entries]),
      (True, True, ["unifiprotect"]))
# 13 · 906
p = Platform(found="unifiprotect")
r = L.ensure(store_key(), p.req, p.ws, NET, app="protect")
check("13 · the platform already found the console: its found set-up is carried on, no second one started",
      (r.get("ok"), "POST " + L.FLOW in p.writes, p.writes), (True, False, ["GET " + L.FLOW + "/d1", "POST " + L.FLOW + "/d1"]))

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
