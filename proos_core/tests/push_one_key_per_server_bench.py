"""ONE PUSH KEY PER APPLE SERVER (16 Oct 2026, register 893).
Run: python3 tests/push_one_key_per_server_bench.py

Read on Dave's box 16 Oct: every alert to a developer build (…ProOS.dev, Apple's test server) was delivered; every
alert to the TestFlight/App Store builds (…ProOS, …ProOSPro, Apple's live server) was refused with
BadEnvironmentKeyInToken. Apple now issues keys that work on one server only, and Core held one key for both.

This imports Core's server module and RUNS _apns_cred and _apns_config_status against a fake credential store:
  1  a key loaded for both servers (the old way) still signs every phone — nothing already working changes
  2  a live-server key signs the shop builds; the test-server key the developer builds
  3  a server with no key of its own falls back to the both-servers key, never to the other server's key
  4  the status says which server each key is for, and whether every phone can be reached
  5  a key loaded "for both" that Apple has REFUSED on a server does not count as covering it — Apple's reply decides,
     not the label (the key on Dave's box was loaded as one key and works on the test server only)
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PROOS_DATA_DIR", tempfile.mkdtemp(prefix="proos_pushkeys_"))
sys.path.insert(0, os.path.join(HERE, ".."))
PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


# Core's own server module, imported (its main loop only runs as __main__)
_spec = importlib.util.spec_from_file_location("server", os.path.join(HERE, "..", "server.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)
check("the key choice is in Core (slots, choice, status)",
      all(hasattr(S, n) for n in ("_APNS_SLOTS", "_apns_cred", "_apns_config_status")))


class Creds:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return (self.d.get(k) or {}).get("p8")

    def meta(self, k):
        return (self.d.get(k) or {}).get("meta", {})

    def put(self, k, p8, key_id):
        self.d[k] = {"p8": p8, "meta": {"key_id": key_id, "team_id": "TEAM123456"}}


C = Creds()
STATUS = {}
S._creds = C                       # a fake credential store in place of the box's
S.push_status = lambda: STATUS     # and the last answers Apple gave, as the bench sets them
cred, status = S._apns_cred, S._apns_config_status

# 1 · the old way: one key for both
C.put("apns", "PEM-BOTH", "BOTHKEY001")
check("1 · a both-servers key signs a developer build", (cred("sandbox") or {}).get("key_id"), "BOTHKEY001")
check("1 · …and a shop build", (cred("production") or {}).get("key_id"), "BOTHKEY001")
check("1 · the status reads as before at the top (one key loaded)", (status()["set"], status()["key_id"]), (True, "BOTHKEY001"))

# 2 · one key per server
C.put("apns_production", "PEM-LIVE", "LIVEKEY001")
C.put("apns_sandbox", "PEM-TEST", "TESTKEY001")
check("2 · a shop build (live server) is signed with the live-server key", (cred("production") or {}).get("key_id"), "LIVEKEY001")
check("2 · a developer build (test server) with the test-server key", (cred("sandbox") or {}).get("key_id"), "TESTKEY001")
check("2 · the credential says which server it was chosen for", (cred("production") or {}).get("env"), "production")

# 3 · fallback is the both-servers key, never the other server's
C.d.pop("apns_production")
check("3 · no live key → the both-servers key", (cred("production") or {}).get("key_id"), "BOTHKEY001")
C.d.pop("apns")
check("3 · no live key and no both-servers key → no key (never the test server's key)", cred("production"), None)
check("3 · …while the developer build still has its own", (cred("sandbox") or {}).get("key_id"), "TESTKEY001")

# 4 · the status names each server's key and says whether every phone can be reached
st = status()
check("4 · the status lists the key for each server", st["keys"]["sandbox"]["key_id"], "TESTKEY001")
check("4 · …and says the live server is NOT covered — the case on Dave's box", st["covers"], {"production": False, "sandbox": True})
C.put("apns_production", "PEM-LIVE", "LIVEKEY001")
check("4 · loading a live key covers both", status()["covers"], {"production": True, "sandbox": True})
check("4 · the key itself never leaves Core (no p8 in the status)", "PEM" in repr(status()), False)

# 5 · Apple's answer decides
C.d.clear(); C.put("apns", "PEM-BOTH", "BOTHKEY001")
STATUS.update({"iPhone": {"ok": False, "env": "production", "key_id": "BOTHKEY001", "at": 100,
                          "words": "Apple refused it: BadEnvironmentKeyInToken — …"},
               "iPhone 17 Pro": {"ok": True, "env": "sandbox", "key_id": "BOTHKEY001", "at": 101, "words": "delivered to Apple"}})
st = status()
check("5 · a both-servers key Apple refused on the live server does not cover it", st["covers"], {"production": False, "sandbox": True})
check("5 · …and the status says which key was refused there, and when", st["refused"], {"production": {"key_id": "BOTHKEY001", "at": 100}})
C.put("apns_production", "PEM-LIVE", "LIVEKEY001")
check("5 · a new live-server key is not blamed for the old key's refusal", status()["covers"]["production"], True)
STATUS["iPad"] = {"ok": False, "env": "production", "key_id": "LIVEKEY001", "at": 200, "words": "Apple refused it: BadDeviceToken"}
check("5 · only Apple's environment refusal counts — a bad device token is the phone's problem, not the key's", status()["covers"]["production"], True)

# 6 · REGISTER 897 (read on the box 16 Oct): with a live-server key loaded, the developer builds' record said "no Apple
# push key is loaded" — untrue. The words name the server that has no key.
C.d.clear(); C.put("apns_production", "PEM-LIVE", "LIVEKEY001")
check("6 · a developer build with only a live-server key loaded: told the TEST server's key is missing",
      S._no_key_words("sandbox").startswith("no key for Apple's test server is loaded — developer builds"), True)
C.d.clear(); C.put("apns_sandbox", "PEM-TEST", "TESTKEY001")
check("6 · a shop build with only a test-server key: told the LIVE server's key is missing",
      "Apple's live server" in S._no_key_words("production"), True)
C.d.clear()
# re-worded, register 1348: the push key lives only on ProOS Connect, so the plain words say where it belongs
check("6 · no key at all: the plain words", S._no_key_words("sandbox").startswith("ProOS Connect doesn't hold the Apple push key"), True)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
