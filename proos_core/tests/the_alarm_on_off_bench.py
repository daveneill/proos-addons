"""ALARM MANAGER ON/OFF — register 1231 (Dave, 6 Nov: "yes all of it please"; each asks first in Pro).
Run: python3 tests/the_alarm_on_off_bench.py

DRIVEN: the REAL uniconsole.send_json over a stand-in console that records what it was sent.
  1. the change goes with ProOS's own console session and its CSRF token, as JSON, to the path asked
  2. send_json only CHANGES (PATCH / PUT / POST) — a GET or DELETE through it is refused before anything is sent
  3. a stale sign-in is renewed once; the console's web page, or a refusal, is never taken as done
  4. the route sends ONLY {"enable": on}, only for an alarm in the console's list right now that UniFi marks editable,
     and says "confirmed" only when the re-read list shows the alarm in the new state (SOURCE)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
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


class Creds:
    def meta(self, k):
        return {"host": "192.168.8.1"}

    def get(self, k):
        return None


SENT = []
REPLIES = []


def http(method, url, body=None, headers=None, timeout=None):
    SENT.append((method, url, body, dict(headers or {})))
    return REPLIES.pop(0) if REPLIES else (200, {"content-type": "application/json"}, b"{}")


uc._SESSION.update({"host": "192.168.8.1", "cookie": "TOKEN=abc", "csrf": "CSRF-1", "at": 9e18})
SENT.clear()
d, why = uc.send_json(Creds(), None, "PATCH", "/proxy/protect/api/automations/a1", {"enable": False}, http=http)
m, url, body, hd = SENT[-1]
ok(why is None and m == "PATCH" and url == "https://192.168.8.1/proxy/protect/api/automations/a1" and body == {"enable": False}
   and hd.get("x-csrf-token") == "CSRF-1" and hd.get("cookie") == "TOKEN=abc",
   "1 · the change goes with ProOS's session and CSRF token, as JSON, to the path asked")
SENT.clear()
r1 = uc.send_json(Creds(), None, "GET", "/x", None, http=http)
r2 = uc.send_json(Creds(), None, "DELETE", "/x", None, http=http)
ok(r1 == (None, "not a change") and r2 == (None, "not a change") and not SENT,
   "2 · only a change goes through it — GET and DELETE are refused before anything is sent")
SENT.clear()
REPLIES[:] = [(401, {}, b""), (200, {"content-type": "text/html"}, b"<html>")]
orig = uc._session
uc._session = lambda creds, h, http=None: ({"cookie": "TOKEN=abc", "x-csrf-token": "CSRF-1"}, None)
d3, why3 = uc.send_json(Creds(), None, "PATCH", "/p", {"enable": True}, http=http)
REPLIES[:] = [(403, {}, b""), (403, {}, b"")]
d4, why4 = uc.send_json(Creds(), None, "PATCH", "/p", {"enable": True}, http=http)
uc._session = orig
ok(len(SENT) == 4 and d3 is None and "answered 200" in why3 and d4 is None and "answered 403" in why4,
   "3 · a stale sign-in is renewed once; a web page or a refusal is never taken as done")
src = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
i = src.index('if method == "POST" and sub == "alarms" and sub2 == "set":')
route = src[i:src.index('return self._send(404, {"error": "unknown console route"})', i)]
# Pin rewritten again (register 1231c — MEASURED on Dave's second press: PATCH → 400 "not available when global alarm
# manager is enabled"; PUT → 404). PATCH {"enable"} is the console's call; the PUT tries are gone; Global mode is named.
ok('_uc.send_json(_creds, ph, "PATCH", "/proxy/protect/api/automations/" + aid, {"enable": on})' in route
   and route.count("send_json(") == 1 and '"PUT"' not in route and 'not x.get("deleted")' in route
   and 'row.get("editable", True)' in route and 'bool(r2.get("enable")) == on' in route and '"confirmed": False' in route
   and '"global alarm manager" in why2.lower()' in route and '"global_mode": glob' in route and "call_service" not in route,
   "4 · only {enable}, only for a listed, editable alarm; UniFi's Global-mode refusal named in words; confirmed only from the re-read list")
REPLIES[:] = [(400, {"content-type": "application/json"}, b'{"error":"conditions must not be empty"}')]
uc._session = lambda creds, h, http=None: ({"cookie": "TOKEN=abc", "x-csrf-token": "CSRF-1"}, None)
_d, why5 = uc.send_json(Creds(), None, "PUT", "/p", {"enable": False}, http=http)
REPLIES[:] = [(400, {"content-type": "text/html"}, b"<html>secret page</html>")]
_d, why6 = uc.send_json(Creds(), None, "PUT", "/p", {"enable": False}, http=http)
uc._session = orig
ok(why5 == "the console answered 400 — conditions must not be empty" and why6 == "the console answered 400" and "secret" not in why6,
   "5 · a refusal carries the console's own short reason (never a web page's text)")
print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
