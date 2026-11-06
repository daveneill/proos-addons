"""UNIFI ACCESS — register 1241 (built before Access is on Dave's console). Run: python3 tests/the_access_bench.py

DRIVEN: the REAL accessviews over records shaped as the official Access API reference documents them, and the REAL
uniconsole.access_call over a stand-in console.
  1. NOTHING secret leaves Core: PINs, NFC card tokens, face and touch-pass data, user ids
  2. doors: locked / unlocked, open / closed in plain fields; people with whether they have a PIN and how many cards (never which)
  3. the log: who, what, which door, the result, when — newest first
  4. access_call: only the Access API's own developer paths, only with the Access token; "not on the console yet" said plainly;
     an Access refusal ("code" not SUCCESS) is a why-not
  5. the change route: unlock is "sent", never confirmed open; a lock rule only from the three offered and confirmed from
     the read-back; lockdown and evacuation never both (SOURCE)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import accessviews as av                                # noqa: E402
from proos import uniconsole as uc                                 # noqa: E402

PASS, FAIL = 0, []


def ok(c, m):
    global PASS
    if c:
        PASS += 1
        print("  ok  " + m)
    else:
        FAIL.append(m)
        print("  FAIL " + m)


DOORS = [{"id": "door0001", "full_name": "Front Gate", "door_lock_relay_status": "lock", "door_position_status": "close", "is_bind_hub": True},
         {"id": "door0002", "name": "Garage", "door_lock_relay_status": "unlock", "door_position_status": "open"}]
USERS = [{"id": "USERID-SECRET", "first_name": "Dave", "last_name": "N", "status": "ACTIVE", "pin_code": {"token": "PIN-SECRET"},
          "nfc_cards": [{"token": "CARD-SECRET", "id": "123"}], "face": "FACE-SECRET", "touch_pass": "TP-SECRET"}]
LOGS = {"hits": [{"_source": {"actor": {"display_name": "Dave N", "id": "USERID-SECRET"}, "event": {"type": "access.door.unlock", "result": "ACCESS", "published": 1762400000},
                              "target": [{"type": "door", "display_name": "Front Gate", "id": "door0001"}, {"type": "UAH", "display_name": "Hub"}]}},
                 {"_source": {"actor": {"display_name": "Visitor"}, "event": {"type": "access.door.unlock", "result": "BLOCKED", "published": 1762400100}, "target": []}}]}
v = [av.doors_view(DOORS), av.people_view(USERS), av.activity_view(LOGS)]
blob = json.dumps(v)
ok(not any(x in blob for x in ("SECRET", "token", '"123"')), "1 · no PIN, card token, face, touch pass or user id leaves Core")
d = {x["name"]: x for x in v[0]}
ok(d["Front Gate"]["locked"] is True and d["Front Gate"]["open"] is False and d["Garage"]["locked"] is False and d["Garage"]["open"] is True
   and not d["Garage"]["has_hub"] and v[1][0] == {"name": "Dave N", "status": "ACTIVE", "number": None, "has_pin": True, "cards": 1},
   "2 · doors locked/open in plain fields; people with whether they have a PIN and how many cards")
ok([x["who"] for x in v[2]] == ["Visitor", "Dave N"] and v[2][1]["where"] == "Front Gate" and v[2][0]["result"] == "BLOCKED",
   "3 · the log: who, what, where, result, newest first")


class Creds:
    def __init__(self, tok):
        self.tok = tok

    def meta(self, k):
        return {"host": "192.168.8.1"}

    def get(self, k):
        return self.tok if k == uc.SVC_ACCESS else None


SENT = []


def http(method, url, body=None, headers=None, timeout=None):
    SENT.append((method, url, dict(headers or {})))
    return R.pop(0)


R = [(200, {}, b'{"code":"SUCCESS","data":[{"id":"door0001"}]}')]
a = uc.access_call(Creds("TOK"), None, "GET", "/api/v1/developer/doors", http=http)
b = uc.access_call(Creds(None), None, "GET", "/api/v1/developer/doors", http=http)
c = uc.access_call(Creds("TOK"), None, "GET", "/api/v1/developer/../../etc", http=http)
R[:] = [(0, {}, b"")]
d0 = uc.access_call(Creds("TOK"), None, "GET", "/api/v1/developer/users", http=http)
R[:] = [(200, {}, b'{"code":"CODE_AUTH_FAILED","msg":"token invalid"}')]
e = uc.access_call(Creds("TOK"), None, "PUT", "/api/v1/developer/doors/door0001/unlock", {}, http=http)
ok(a == ([{"id": "door0001"}], None) and SENT[0][1] == "https://192.168.8.1:12445/api/v1/developer/doors" and SENT[0][2]["Authorization"] == "Bearer TOK"
   and b[0] is None and "token" in b[1] and c == (None, "not an Access call ProOS makes") and len(SENT) == 3
   and d0[0] is None and "not on the console yet" in d0[1] and e[0] is None and "token invalid" in e[1],
   "4 · access_call: only the Access API's own paths with its token; not there yet said plainly; a refusal is a why-not")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "POST" and sub == "access" and sub2 == "act":')
r = src[i:src.index('if method == "POST" and sub == "alarms" and sub2 == "set":', i)]
ok('"confirmed": False' in r and '"unlock sent' in r and "rule not in _av.LOCK_RULES" in r and '_av.LOCK_RULES' in r
   and 'want["lockdown"] and want["evacuation"]' in r and "call_service" not in r and av.LOCK_RULES == ("keep_unlock", "keep_lock", "reset"),
   "5 · unlock is sent, never confirmed open; three lock rules only, confirmed from the read-back; never lockdown and evacuation together")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
