"""PROOS'S KEYS LIVE ON CONNECT, NOT ON ANY BOX — the gateway (register 1347, 14 Nov 2026).
Run: python3 tests/proos_keys_live_on_connect_bench.py

Dave 14 Nov 13:43–13:53: one key in ProOS Connect, none on any box — Claude, ChatGPT, Apple push, Apple Music; "remove it
all from Pro"; and Connect's page in the ProOS look. His 13 Sep ruling, built.

DRIVEN — the real Connect relay (LIVE/connect/relay/relay.py + gateway.py) over real HTTP, the providers stood in:
 1. an approved box's AI request is forwarded with ProOS's key put in — the box's own key headers are dropped — and the
    provider's answer comes back unchanged; a streamed answer streams through
 2. the ledger keeps counts and cost per box per model — read from the provider's own usage, streamed or whole — and
    never a word of the conversation
 3. an unknown box, a pending box and a suspended home get nothing; a door that is not on the list is refused; no key on
    Connect says so
 4. the fleet setting (provider, models) is one answer for every box
 5. push: signed with ProOS's Apple key HERE and sent; no key → said; Apple's refusal → said
 6. Apple Music: a developer token signed here, MUSIC_TOKEN_S long; no key → said
 7. the admin page: every key shows as Set / Not Set — never a character of a key; a bad key is not saved
 8. the page is in the ProOS look (v3 glass, Title Case) — SPELLING
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_D = tempfile.mkdtemp(prefix="proos_gw_")
os.environ["PROOS_CONNECT_DIR"] = os.path.join(_D, "connect")
os.environ["PROOS_CONNECT_HOMES"] = os.path.join(_D, "pub", "homes.caddy")
import importlib.util                                                # noqa: E402

RELAY_DIR = os.path.join(HERE, "..", "..", "connect", "relay")
sys.path.insert(0, RELAY_DIR)
_spec = importlib.util.spec_from_file_location("relay", os.path.join(RELAY_DIR, "relay.py"))
relay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relay)
G = relay.gateway

passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok  " + m)
    else:
        failed += 1
        print("  FAIL " + m)


relay.publish_homes = lambda st=None: "skipped"
relay.time.sleep = lambda s: None
srv = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:%d" % srv.server_address[1]

# the boxes: one approved, one pending, one suspended
st = relay.load()
for bid, status, plan in (("px-good", "approved", "active"), ("px-wait", "pending", "trial"),
                          ("px-off", "approved", "suspended")):
    st["boxes"][bid] = {"secret_hash": relay._h("s-" + bid), "status": status, "plan": plan, "name": bid}
relay.save(st)


def call(method, path, body=None, box="px-good", extra=None, raw=False):
    h = {"Content-Type": "application/json"}
    if box:
        h.update({"X-ProOS-Box": box, "Authorization": "Bearer s-" + box})
    h.update(extra or {})
    req = urllib.request.Request(B + path, data=(body if isinstance(body, bytes) else
                                                 json.dumps(body).encode()) if body is not None else None,
                                 method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            return r.status, (data if raw else json.loads(data or b"{}"))
    except urllib.error.HTTPError as e:
        data = e.read()
        return e.code, (data if raw else json.loads(data or b"{}"))


# ── a stand-in provider ──────────────────────────────────────────────────────
SEEN = []


class Resp:
    def __init__(self, status, ctype, chunks):
        self.status, self.ctype, self.chunks = status, ctype, list(chunks)

    def getheader(self, k, d=None):
        return self.ctype if k.lower() == "content-type" else d

    def read1(self, n=-1):
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, n=-1):
        out = b"".join(self.chunks)
        self.chunks = []
        return out


class Conn:
    def close(self):
        pass


ANSWER = {"id": "msg_1", "content": [{"type": "text", "text": "The Office light is off."}],
          "usage": {"input_tokens": 1000, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 200,
                    "output_tokens": 50}}
SSE = [b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1200,'
       b'"cache_read_input_tokens":30000,"cache_creation_input_tokens":0,"output_tokens":1}}}\n\n',
       b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}\n\n',
       b'data: {"type":"message_delta","usage":{"output_tokens":80}}\n\n']
MODE = {"r": None}


def fake_open(prov, rest, body, headers, key, timeout=180):
    SEEN.append({"prov": prov, "rest": rest, "body": body, "headers": dict(headers), "key": key,
                 "auth": G.PROVIDERS[prov]["auth"](key)})
    return Conn(), MODE["r"]


G.open_provider = fake_open

# 3 · no key yet
code, r = call("POST", "/v1/ai/anthropic/v1/messages", {"model": "claude-opus-5-5", "messages": []})
ok(code == 503 and "not set up" in r.get("error", ""), "3 · no Claude key on Connect: said plainly (%s)" % code)

st = relay.load()
st["keys"] = {"anthropic": "sk-ant-PROOS-OWN-KEY", "openai": "sk-PROOS-OPENAI"}
relay.save(st)

# 1 · whole answer
MODE["r"] = Resp(200, "application/json", [json.dumps(ANSWER).encode()])
body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "is the office light on?"}]}
code, r = call("POST", "/v1/ai/anthropic/v1/messages", body,
               extra={"x-api-key": "BOX-OWN-KEY", "anthropic-version": "2023-06-01"})
s = SEEN[-1]
ok(code == 200 and r == ANSWER, "1 · the provider's answer comes back unchanged")
ok(s["key"] == "sk-ant-PROOS-OWN-KEY" and s["auth"] == {"x-api-key": "sk-ant-PROOS-OWN-KEY"}
   and json.loads(s["body"]) == body and s["rest"] == "/v1/messages",
   "  …forwarded byte-for-byte with ProOS's key put in")
fwd = {k.lower(): v for k, v in G.PROVIDERS["anthropic"]["auth"]("k").items()}
passed_hdrs = {k.lower() for k in s["headers"] if k.lower() in G.PASS_HEADERS}
ok("x-api-key" not in passed_hdrs and "authorization" not in passed_hdrs and "anthropic-version" in passed_hdrs,
   "  …the box's own key headers are never passed on; the provider's version header is")

# 1 · streamed answer
MODE["r"] = Resp(200, "text/event-stream", SSE)
code, raw = call("POST", "/v1/ai/anthropic/v1/messages", dict(body, stream=True), raw=True)
ok(code == 200 and raw == b"".join(SSE), "  …a streamed answer streams through whole")

# 2 · ledger
st = relay.load()
row = st["ledger"][relay.time.strftime("%Y-%m")]["px-good"]["ai:claude-opus-5-5"]
ok(row["calls"] == 2 and row["fresh"] == 1200 + 1200 and row["cached"] == 9000 + 30000 and row["out"] == 50 + 80,
   "2 · the ledger reads the provider's own usage, whole and streamed: %s" % {k: row[k] for k in ("calls", "fresh", "cached", "out")})
want = round((2400 * 4.0 + 39000 * 0.20 + 130 * 20.0) / 1e6, 6)
ok(abs(row["usd"] - want) < 1e-6, "  …and its cost at the published price (US$%.4f)" % row["usd"])
blob = json.dumps(st["ledger"])
ok("office light" not in blob.lower() and "Hel" not in blob and "messages" not in blob,
   "  …and not a word of the conversation")
code, r = call("GET", "/v1/usage")
ok(code == 200 and r["usage"]["calls"] == 2 and abs(r["usage"]["usd"] - round(want, 4)) < 1e-4,
   "  …the box reads its own month back (what its Pro shows)")

# OpenAI: pictures counted
MODE["r"] = Resp(200, "application/json", [json.dumps({"data": [{"b64_json": "x"}], "usage": {}}).encode()])
code, _ = call("POST", "/v1/ai/openai/v1/images/generations", {"model": "gpt-image-2", "prompt": "a living room"})
st = relay.load()
img = st["ledger"][relay.time.strftime("%Y-%m")]["px-good"]["ai:gpt-image-2"]
ok(code == 200 and SEEN[-1]["auth"] == {"Authorization": "Bearer sk-PROOS-OPENAI"} and img["images"] == 1
   and abs(img["usd"] - G.IMAGE_USD) < 1e-9, "  …ChatGPT's door too: a scene picture is counted as a picture")

# 3 · refusals
n = len(SEEN)
for who, label in (("px-wait", "a pending box"), ("px-off", "a suspended home"), ("px-nobody", "an unknown box")):
    code, r = call("POST", "/v1/ai/anthropic/v1/messages", body, box=who)
    ok(code == 403 and len(SEEN) == n, "3 · %s gets nothing (%s: %s)" % (label, code, r.get("error")))
code, _ = call("POST", "/v1/ai/anthropic/v1/files", body)
code2, _ = call("POST", "/v1/ai/elsewhere/v1/messages", body)
ok(code == 404 and code2 == 404 and len(SEEN) == n, "  …a door not on the list is refused")

# 4 · the fleet setting
code, r = call("GET", "/v1/ai/policy")
ok(code == 200 and r["provider"] == "anthropic" and r["ready"]["anthropic"] and r["ready"]["openai"]
   and not r["ready"]["push"], "4 · one fleet setting, and what Connect is ready to carry")

# 5 · push
code, r = call("POST", "/v1/push", {"token": "abc123", "payload": {"aps": {"alert": "Front door"}}})
ok(code == 503 and "push key" in r.get("error", r.get("said", "")) or (code == 503 and not r.get("ok")),
   "5 · no Apple push key yet: said (%s)" % code)
st = relay.load()
st["keys"].update({"apns_p8": "-----BEGIN PRIVATE KEY-----x", "apns_key_id": "ABCDE12345", "apns_team_id": "TEAM123456",
                   "apns_topic": "au.com.protechsystems.proos"})
relay.save(st)
SENT = []
_ps = G.push_send
G.push_send = lambda keys, token, payload, topic=None, sandbox=False: _ps(
    keys, token, payload, topic, sandbox, signer=lambda si, pem: b"\x01" * 64,
    post=lambda host, t, jwt, top, pl: (SENT.append((host, t, jwt, top, pl)) or (200, "")))
code, r = call("POST", "/v1/push", {"token": "abc123", "payload": {"aps": {"alert": "Front door"}}})
h, t, jwt, top, pl = SENT[-1]
ok(code == 200 and r["ok"] and h == "api.push.apple.com" and t == "abc123" and top == "au.com.protechsystems.proos"
   and jwt.count(".") == 2 and json.loads(pl)["aps"]["alert"] == "Front door",
   "5 · an alert is signed HERE with ProOS's key and sent to Apple")
G.push_send = lambda keys, token, payload, topic=None, sandbox=False: (400, '{"reason":"BadDeviceToken"}')
code, r = call("POST", "/v1/push", {"token": "zzz", "payload": {"aps": {}}})
ok(code == 502 and r["apple"] == 400 and "BadDeviceToken" in r["said"], "  …Apple's refusal is passed back in its words")

# 6 · Apple Music
code, r = call("GET", "/v1/music/token")
ok(code == 503, "6 · no MusicKit key yet: said")
st = relay.load()
st["keys"].update({"music_p8": "-----BEGIN PRIVATE KEY-----y", "music_key_id": "MUSIC12345", "music_team_id": "TEAM123456"})
relay.save(st)
_mt = G.music_token
G.music_token = lambda keys: _mt(keys, signer=lambda si, pem: b"\x02" * 64)
code, r = call("GET", "/v1/music/token")
import base64  # noqa: E402
claims = json.loads(base64.urlsafe_b64decode(r["token"].split(".")[1] + "=="))
ok(code == 200 and claims["iss"] == "TEAM123456" and claims["exp"] - claims["iat"] == G.MUSIC_TOKEN_S
   and r["expires"] == claims["exp"], "6 · a short-lived Apple Music developer token, signed here")

# 6b · photo search (register 1348): ProOS's one Unsplash key, only Unsplash's search and its "picked" call
code, r = call("GET", "/v1/photos/search?query=beach")
ok(code == 503 and "photo search key" in r["error"], "6b · no Unsplash key yet: said")
st = relay.load()
st["keys"]["unsplash"] = "UNSPLASH-ACCESS"
relay.save(st)
PH = []
G.photos_get = lambda key, path_q, opener=None: (PH.append((key, path_q)), (True, 200, b'{"results":[]}'))[1]
code, r = call("GET", "/v1/photos/search?query=beach+house&per_page=15")
code2, r2 = call("GET", "/v1/photos/download?photo=abc123&ixid=Z")
code3, r3 = call("GET", "/v1/photos/download?photo=../me")
code4, r4 = call("GET", "/v1/photos/search?query=x", box="px-off")
ok(code == 200 and r == {"results": []} and PH[0] == ("UNSPLASH-ACCESS", "/search/photos?query=beach+house&per_page=15")
   and code2 == 200 and PH[1][1] == "/photos/abc123/download?ixid=Z" and code3 == 400 and code4 == 403 and len(PH) == 2,
   "6b · a search and a pick go to Unsplash with ProOS's key; a bad photo id and a suspended home are refused")
ok(not G.photo_path_ok("/users/me") and not G.photo_path_ok("/photos/a/b/download") and G.photo_path_ok("/search/photos?q=x"),
   "  …only Unsplash's search and its 'a photo was picked' address — nothing else of Unsplash")
code, r = call("GET", "/v1/ai/policy")
ok(r["ready"]["photos"] is True, "  …and Connect tells the box it holds the photo search key")

# 7 · the admin page never shows a key
st = relay.load()
st["admin"] = relay._pw_hash("a-long-password")
relay.save(st)
with relay._lock:
    relay._sessions["sid1"] = relay.time.time() + 600
req = urllib.request.Request(B + "/admin", headers={"Cookie": "pc=sid1"})
page = urllib.request.urlopen(req, timeout=10).read().decode()
ok("sk-ant-PROOS-OWN-KEY" not in page and "sk-PROOS-OPENAI" not in page and "BEGIN PRIVATE KEY" not in page
   and "MUSIC12345" not in page and page.count('class="chip ok">Set') >= 6,
   "7 · the admin page shows each key as Set — never a character of one")
ok("Claude Key" in page and "ChatGPT Key" in page and "Push Key (.p8)" in page and "MusicKit Key (.p8)" in page
   and "Save for Every Box" in page and "Use This Month" in page,
   "  …every key and the fleet setting have their place on it")
G.check_key = lambda which, value: (False, "the provider answered 401")
form = "which=anthropic&value=sk-bad".encode()
req = urllib.request.Request(B + "/admin/keys", data=form, method="POST",
                             headers={"Cookie": "pc=sid1", "Content-Type": "application/x-www-form-urlencoded"})
page = urllib.request.urlopen(req, timeout=10).read().decode()
ok("Not saved" in page and relay.load()["keys"]["anthropic"] == "sk-ant-PROOS-OWN-KEY",
   "  …a key the provider refuses is not saved, and the good one stays")

# 8 · the look
P = relay.PAGE
ok("backdrop-filter:blur" in P and "--blue:#2b4bff" in P and "PRO<b>OS</b>" in open(os.path.join(RELAY_DIR, "relay.py")).read()
   and "Pro Assist" in open(os.path.join(RELAY_DIR, "relay.py")).read(),
   "8 · the page is in the ProOS look: glass cards, the ProOS blue, the mark, Title Case (SPELLING)")
inst = open(os.path.join(RELAY_DIR, "install.sh")).read()
ok("install -m 644 gateway.py /opt/proos-connect/gateway.py" in inst, "  …and the installer puts gateway.py beside the relay")

srv.shutdown()
print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
