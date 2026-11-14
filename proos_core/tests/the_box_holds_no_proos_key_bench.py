"""THE BOX HOLDS NO KEY OF PROOS'S OWN — register 1347, the box's side (14 Nov 2026).
Run: python3 tests/the_box_holds_no_proos_key_bench.py

Dave 14 Nov: "just build the whole lot including the ChatGPT option to run on ProConnect and remove it all from Pro".
The relay's side (one key per supplier on ProOS Connect, counted per home) is proos_keys_live_on_connect_bench. This is
the box's: every call that needs ProOS's key goes to Connect with the box's own credential, and Connect adds the key.

DRIVEN (Assist's real config, wire and HTTP helper; the real scene-picture call; Apple Music's real token reader; the
real push hand-off — each with the network replaced by a recorder):
  1. Connect has said nothing → the box's own key, said as "box"; no key anywhere → Assist is honestly off
  2. Connect holds the AI key → its AI and model are this box's, the active key is Connect's marker, said as "connect"
  3. what the box saves is still the box's own settings — Connect's choice is never written into assist.json
  4. a real Assist call with the marker goes to ProOS Connect: the box's id and secret, no key, the provider's own
     version header kept; a call carrying a real key goes to the provider unchanged
  5. REGISTER 1348: once Connect holds a key, the box's own copy is REMOVED (AI keys, picture key, photo search key) —
     and nothing is removed for a key Connect does not hold yet; the old Developer switch is gone
  6. scene pictures: Connect first when it holds the OpenAI key; the picture request goes to Connect
  7. Apple Music: the token is Connect's when it holds the MusicKit key, and Pro is told so
  8. iPhone alerts: Connect's answer comes back in push.send's own shape (delivered; a dead phone flagged)
  9. photo search (1348): Connect first when it holds the Unsplash key; the search and Unsplash's "picked" call go to
     ProOS Connect, never to Unsplash with a key from the box
 10. Connect is asked at most once per refresh window, and only by a box with a Connect identity
"""
import io
import json
import os
import sys
import tempfile
import time

D = tempfile.mkdtemp(prefix="proos_nokey_")
os.environ["PROOS_DATA_DIR"] = D
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import assist as A, connectkeys as CK, connectvoice as CV, applemusic as AM, scenephotos as SP  # noqa: E402

passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok  " + m)
    else:
        failed += 1
        print("  FAIL " + m)


def box_cfg(d):
    with open(os.path.join(D, "assist.json"), "w") as fh:
        json.dump(d, fh)


def policy(ready, provider="anthropic", model="claude-opus-5-5"):
    with open(CK.POLICY_PATH, "w") as fh:
        json.dump({"provider": provider, "model": model, "ready": ready, "read_at": time.time()}, fh)


ident = CV.identity()

# 1
box_cfg({"provider": "openai", "model": "gpt-6-astra", "keys": {"openai": "sk-box-own"}})
c = A.load_config()
ok(c["provider"] == "openai" and c["api_key"] == "sk-box-own" and c["via"] == "box",
   "1 · Connect has said nothing: the box's own key, said as 'box'")
box_cfg({})
ok(A.status()["enabled"] is False and A.load_config()["via"] == "",
   "  …no key on the box and none on Connect: Assist is off, and says so")

# 2
box_cfg({"provider": "openai", "model": "gpt-6-astra", "keys": {"openai": "sk-box-own"}})
policy({"anthropic": True, "openai": True, "push": True, "music": True})
c = A.load_config()
ok(c["provider"] == "claude" and c["model"] == "claude-opus-5-5" and c["api_key"] == CK.CONNECT and c["via"] == "connect",
   "2 · Connect holds the AI key: Claude Opus 5.5 as the fleet says, Connect's marker, said as 'connect'")
policy({"anthropic": False, "openai": True})
ok(A.load_config()["api_key"] == "sk-box-own",
   "  …Connect runs Claude but does not hold its key yet: the box's own key, nothing breaks while the keys move")
policy({"anthropic": True, "openai": True, "push": True, "music": True})

# 3
A.save_config({"photo_key": "unsplash-1"})
with open(os.path.join(D, "assist.json")) as fh:
    saved = json.load(fh)
ok(saved.get("provider") == "openai" and saved.get("model") == "gpt-6-astra" and "api_key" not in saved
   and CK.CONNECT not in json.dumps(saved),
   "3 · a save writes the box's own settings back — never Connect's AI, never the marker")

# 4
sent = []


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


real_urlopen = A.urllib.request.urlopen
A.urllib.request.urlopen = lambda req, timeout=None: (sent.append(req), _Resp(b'{"content":[{"type":"text","text":"ok"}]}'))[1]
try:
    A._http_json("https://api.anthropic.com/v1/messages", {"model": "m"},
                 {"x-api-key": CK.CONNECT, "anthropic-version": "2023-06-01"})
    A._http_json("https://api.openai.com/v1/responses", {"model": "m"}, {"Authorization": "Bearer sk-real"})
finally:
    A.urllib.request.urlopen = real_urlopen
h0 = {k.lower(): v for k, v in sent[0].header_items()}
ok(sent[0].full_url == CV.CONNECT_URL + "/v1/ai/anthropic/v1/messages"
   and h0.get("x-proos-box") == ident["box_id"] and h0.get("authorization") == "Bearer " + ident["secret"]
   and "x-api-key" not in h0 and h0.get("anthropic-version") == "2023-06-01",
   "4 · a real Assist call with the marker goes to ProOS Connect with the box's credential — no key, version kept")
h1 = {k.lower(): v for k, v in sent[1].header_items()}
ok(sent[1].full_url == "https://api.openai.com/v1/responses" and h1.get("authorization") == "Bearer sk-real",
   "  …a call carrying a real key goes to the provider exactly as before")
try:
    CK.wire("https://example.com/x", {"x-api-key": CK.CONNECT})
    ok(False, "  …the marker never leaves for anywhere Connect does not carry")
except RuntimeError:
    ok(True, "  …the marker never leaves for anywhere Connect does not carry")

# 5
box_cfg({"provider": "openai", "keys": {"openai": "sk-box-own", "claude": "sk-ant-own"}, "image_key": "sk-img",
         "photo_key": "unsplash-1", "realtime_enabled": False, "ai_source": "box"})
gone = A.retire_box_keys({"anthropic": True, "openai": False, "photos": True})
with open(os.path.join(D, "assist.json")) as fh:
    saved = json.load(fh)
ok(sorted(gone) == ["claude key", "photo search key"] and saved["keys"] == {"openai": "sk-box-own"}
   and saved.get("image_key") == "sk-img" and "photo_key" not in saved and "ai_source" not in saved
   and saved.get("realtime_enabled") is False,
   "5 · Connect holds Claude and photo search: those box keys go; the OpenAI ones stay until Connect holds them")
gone = A.retire_box_keys({"anthropic": True, "openai": True, "photos": True})
with open(os.path.join(D, "assist.json")) as fh:
    saved = json.load(fh)
ok(sorted(gone) == ["openai key", "picture key"] and not saved["keys"] and "image_key" not in saved
   and A.retire_box_keys({"anthropic": True, "openai": True, "photos": True}) == [],
   "  …and when it holds OpenAI's too, the box holds no key at all; asking again removes nothing more")
ok("ai_source" not in A.status(full=True), "  …the Developer switch is gone from what Pro is told")
box_cfg({"provider": "openai", "model": "gpt-6-astra", "keys": {"openai": "sk-box-own"}})

# 6
ok(A._image_key(None) == CK.CONNECT, "6 · scene pictures: Connect's, while it holds the OpenAI key")
pics = []
real_sp = SP.urllib.request.urlopen
SP.urllib.request.urlopen = lambda req, timeout=None: (pics.append(req), _Resp(b'{"data":[{"b64_json":"aGk="}]}'))[1]
try:
    png, err = SP._one_image("gpt-image-2", "a room", "1536x1024", CK.CONNECT)
finally:
    SP.urllib.request.urlopen = real_sp
ok(png == b"hi" and pics[0].full_url == CV.CONNECT_URL + "/v1/ai/openai/v1/images/generations",
   "  …and the picture request goes to ProOS Connect")

# 7
import base64  # noqa: E402
_b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
tok = _b({"alg": "ES256", "kid": "MUSICKEY01"}) + "." + _b({"iss": "TEAM000001", "exp": int(time.time()) + 43200}) + ".sig"
CK._music.update(token=None, expires=0, failed=0)
real_get = CK._get
CK._get = lambda path, timeout=8: {"token": tok, "expires": time.time() + 43200} if path == "/v1/music/token" else {}
try:
    st = AM.status()
finally:
    CK._get = real_get
ok(st.get("set") and st.get("via") == "connect" and st.get("key_id") == "MUSICKEY01" and AM.developer_token() == tok,
   "7 · Apple Music: the token is made on Connect, and Pro is told 'made by ProOS Connect'")

# 8
res = CK.push("abc", b'{"aps":{"alert":"hi"}}', "com.x.ProOS", False, post=lambda: {"ok": True, "apple": 200})
dead = CK.push("abc", b'{"aps":{}}', "com.x.ProOS", False,
               post=lambda: {"ok": False, "apple": 410, "said": '{"reason":"Unregistered"}'})
down = CK.push("abc", b'{"aps":{}}', "com.x.ProOS", False, post=lambda: {"ok": False, "error": "unreachable"})
ok(res == {"ok": True, "status": 200, "via": "connect"} and dead["unregistered"] and dead["status"] == 410
   and not down["ok"] and down["reason"] == "unreachable",
   "8 · iPhone alerts: delivered, a dead phone flagged for pruning, an unreachable Connect said in words")

# 9
from proos import photosearch as PS  # noqa: E402
policy({"anthropic": True, "openai": True, "push": True, "music": True, "photos": True})
ok(A._photo_key(None) == CK.CONNECT, "9 · photo search: Connect's, while it holds the Unsplash key")
seen = []
res = PS.search("beach house", A._photo_key(None),
                opener=lambda req, timeout=None: (seen.append(req), _Resp(b'{"results":[{"urls":{"raw":"https://images.unsplash.com/x"},'
                                                                         b'"links":{"download_location":"https://api.unsplash.com/photos/abc123/download?ixid=Z"}}]}'))[1])
PS.track_use(res["photos"][0]["track"], A._photo_key(None), opener=lambda req, timeout=None: (seen.append(req), _Resp(b"{}"))[1])
h = {k.lower(): v for k, v in seen[0].header_items()}
ok(seen[0].full_url.startswith(CV.CONNECT_URL + "/v1/photos/search?") and "query=beach+house" in seen[0].full_url
   and h.get("x-proos-box") == ident["box_id"] and "client-id" not in json.dumps(h).lower()
   and seen[1].full_url == CV.CONNECT_URL + "/v1/photos/download?photo=abc123&ixid=Z",
   "  …the search and Unsplash's 'a photo was picked' both go to ProOS Connect with the box's credential, no key")

# 10
os.remove(CK.POLICY_PATH)
asked = []
real_refresh = CK.refresh
CK.refresh = lambda fetch=None: asked.append(1)
CK._refreshing.update(on=False, at=0.0)
try:
    CK.policy()
    time.sleep(0.2)
    CK.policy()
    time.sleep(0.2)
    n_with = len(asked)
    os.rename(CV.STATE_PATH, CV.STATE_PATH + ".away")
    CK._refreshing.update(on=False, at=0.0)
    CK.policy()
    time.sleep(0.2)
    os.rename(CV.STATE_PATH + ".away", CV.STATE_PATH)
finally:
    CK.refresh = real_refresh
ok(n_with == 1 and len(asked) == 1,
   "10 · Connect is asked once per refresh window, and never by a box with no Connect identity")

print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
