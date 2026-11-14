"""A PICTURE CAN BE ANYTHING: GENERATE TAKES THE PERSON'S OWN WORDS; SEARCH PHOTOS FINDS REAL ONES — register 1281.
Run: python3 tests/pictures_are_anything_bench.py

Dave, 10 Nov 2026 08:33: backgrounds for areas, scenes and the Home page "to be a search for anything not just a home …
high quality images even generated images using the CHATGPT so if someone described a background they want it will generate
a background to use and also abviously if you typed Aston Martin then it will give you high quality images of an aston
martin"; 08:47 "all option added as long as the UnSplash or Pexels there is no watermarks etc".
BEFORE: Generate wrapped every description as "a premium interior photograph … for a smart-home scene … no people, no text
or logos" (areas the same) — so "Aston Martin" became a room; and there was no search, only a fixed curated set.

DRIVEN: the real photosearch.search with Pexels stood in; the real build_prompt of scenes and areas; Core's settings save.
 1. typed words are the picture: sent as written, only "high quality, wide" added — scenes and areas alike
 2. no words: the name still gets its interior photograph (a bedroom gets a bedroom)
 3. search (Unsplash, 1282) returns each photo's picture, photographer and links with Unsplash's referral; a pick is reported
 4. no key, a refused key, nothing typed, no internet: a plain sentence, never a crash
 5. the key is saved beside the image key, reported only as "saved", never returned
 6. (SOURCE) all three editors — Pro's area/Home editor, Pro's Scene Art, the dashboard's scene editor — have Search
    Photos wired to their own describe box, strip and pick
"""
import io
import json
import os
import sys
import tempfile
import urllib.error

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_pics_")
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import photosearch as PS, scenephotos as SP, roomart as RA, assist as A   # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "\n        wanted %r\n        got    %r" % (want, got))


aston = SP.build_prompt("Work", "Aston Martin DB12 in a garage at night")
check("1 · a scene: the typed words are the picture, only 'high quality, wide' added",
      (aston.startswith("Aston Martin DB12 in a garage at night."), "interior" in aston, "smart-home" in aston,
       "High-quality" in aston), (True, False, False, True))
forest = RA.build_prompt("Bedroom", "a misty forest at dawn")
check("1 · an area (and the Home page) the same", (forest.startswith("a misty forest at dawn."), "interior" in forest), (True, False))
check("2 · no words: a bedroom still gets a bedroom", "bedroom" in RA.build_prompt("Bedroom", "").lower()
      and "interior" in RA.build_prompt("Bedroom", "").lower(), True)

SEEN = {}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# RE-PINNED, register 1282, with cause (Dave 10 Nov 10:15: Pexels has paused new keys; 10:24 he holds an Unsplash demo key):
# the source is Unsplash, answering in Unsplash's own shape.
def ok_opener(req, timeout=0):
    SEEN["url"], SEEN["auth"] = req.full_url, req.headers.get("Authorization")
    return Resp(json.dumps({"results": [{"alt_description": "a silver sports car",
        "urls": {"raw": "https://images.unsplash.com/photo-1?ixid=x", "small": "https://images.unsplash.com/photo-1?w=400"},
        "user": {"name": "Jo Bloggs", "links": {"html": "https://unsplash.com/@jo"}},
        "links": {"html": "https://unsplash.com/photos/1", "download_location": "https://api.unsplash.com/photos/1/download?ixid=x"}}]}).encode())


r = PS.search("Aston Martin", "KEY123", opener=ok_opener)
p = (r.get("photos") or [{}])[0]
check("3 · a search returns the picture (from Unsplash's own address, sized for a background), photographer and link",
      (p.get("thumb"), p.get("url"), p.get("credit")),
      ("https://images.unsplash.com/photo-1?w=400", "https://images.unsplash.com/photo-1?ixid=x&w=2400&q=80&fm=jpg&fit=max", "Jo Bloggs"))
check("3 · …the photographer and Unsplash links carry the referral Unsplash asks for",
      (p.get("credit_url"), p.get("link"), r.get("source")),
      ("https://unsplash.com/@jo?utm_source=ProOS&utm_medium=referral", "https://unsplash.com/photos/1?utm_source=ProOS&utm_medium=referral",
       {"name": "Unsplash", "url": "https://unsplash.com/?utm_source=ProOS&utm_medium=referral"}))
check("3 · …asked of Unsplash's own search, by the box's key, landscape",
      (SEEN["url"].startswith("https://api.unsplash.com/search/photos?"), "query=Aston+Martin" in SEEN["url"],
       "orientation=landscape" in SEEN["url"], SEEN["auth"]), (True, True, True, "Client-ID KEY123"))
SEEN.clear()
t = PS.track_use(p["track"], "KEY123", opener=ok_opener)
check("3 · picking a photo tells Unsplash (its rule), by the photo's own download address",
      (t.get("ok"), SEEN.get("url")), (True, "https://api.unsplash.com/photos/1/download?ixid=x"))
SEEN.clear()
check("3 · …and that call can only ever go to Unsplash's photos address", (PS.track_use("http://192.168.8.1/admin", "K", opener=ok_opener).get("error"), SEEN), ("not an Unsplash photo", {}))


def refuse(code):
    def o(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
    return o


def offline(req, timeout=0):
    raise OSError("no route")


check("4 · no key: says where it is set up (1348: on ProOS Connect, never on the box)", (PS.search("car", "").get("needs_key"), "ProOS Connect" in PS.search("car", "")["error"]), (True, True))
check("4 · a refused key: says so", "refused ProOS's key" in PS.search("car", "K", opener=refuse(401))["error"])
check("4 · the demo key's hour used up: says so", "hourly limit" in PS.search("car", "K", opener=refuse(403))["error"])
check("4 · nothing typed: asks for words", "Type what you are looking for" in PS.search("  ", "K")["error"])
check("4 · no internet: a plain sentence", "Couldn't reach Unsplash" in PS.search("car", "K", opener=offline)["error"])

A.save_config({"photo_key": "PEXELS-SECRET"})
st = A.status(full=True)
check("5 · the key is kept on the box and reported only as saved", (A._photo_key(None), st.get("has_photo_key"),
      "PEXELS-SECRET" in json.dumps(st)), ("PEXELS-SECRET", True, False))

ROOT = os.path.join(HERE, "..", "..", "deploy")
pro = open(os.path.join(ROOT, "pro.html"), encoding="utf-8").read()
dash = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
# RE-PINNED, register 1283, with cause (Dave 10 Nov 11:06 "i think it will be less confusing one button find"): ONE Find
# button per editor — real photos, then a "Create one with AI" tile running that editor's own Generate on the same words.
check("6 · (SOURCE) Pro's area / Home editor: one Find on its own box, strip, pick and Generate",
      "proosPhotoSearch('re-bg-desc','re-bg-styles','re-bg-note','pickRoomBgStyle','re-bg-findbtn','roomBgGenerate')" in pro, True)
check("6 · (SOURCE) Pro's Scene Art: the same",
      "proosPhotoSearch('sa-desc','sa-styles','sa-note','pickSceneArtStyle','sa-findbtn','sceneArtGenerate')" in pro, True)
check("6 · (SOURCE) the dashboard's scene editor: the same",
      "proosPhotoSearch('scene-edit-photoq','scene-edit-results','scene-edit-note','sceneEditPick','scene-edit-findbtn','sceneEditGenerate')" in dash, True)
check("6 · (SOURCE) no editor keeps a separate Generate or Search Photos button",
      [x for x in ('id="re-bg-genbtn"', 'id="sa-genbtn"', 'id="re-bg-searchbtn"', 'id="sa-searchbtn"') if x in pro]
      + [x for x in ('id="scene-edit-genbtn"', 'id="scene-edit-searchbtn"') if x in dash], [])
# RE-PINNED, register 1284, with cause (Dave 11:51 "just have find and separate the results … generate takes longer"; 11:54
# "have the background preview have generating while its generating"). Find starts BOTH; the made picture is the first tile
# ("Creating…" until it lands), the preview says "Creating your picture…", and a picked photo is never overwritten.
check("6 · (SOURCE) Find starts the made picture without waiting, then the photos, the made one first",
      all("_psMake(genFn);" in t and "made+ph.map(" in t and "Creating…" in t for t in (pro, dash)), True)
check("6 · (SOURCE) the preview says a picture is coming, and a picked photo is never overwritten by it",
      all("Creating your picture…" in t and "if(!window._psPicked) use();" in t and "window._psPicked=true;" in t
          and "if(tok!==window._psTok) return;" in t for t in (pro, dash)), True)
check("6 · (SOURCE) no AI button or tile to tap first", [t for t in (pro, dash) if "✦ Create one with AI" in t], [])
check("6 · (SOURCE) both pages ask Core's search, report a pick, and credit the photographer — the source named by Core",
      all("/photos/search?q=" in t and "/photos/used" in t and "Photo by <a href=" in t and "(L.source||{}).name" in t
          for t in (pro, dash)), True)
# REGISTER 1348 (Dave 14 Nov 15:04, "no longer require an API key anywhere"): the key lives on ProOS Connect, so the
# pin is turned round — Pro has NO photo key field, and the Connect page names Photo Search among what Connect carries.
check("6 · (SOURCE) Pro has no photo key field (1348: the key is on ProOS Connect); its Connect panel lists Photo Search",
      'id="as-photokey"' not in pro and "chip(rd.photos,'Photo Search')" in pro, True)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
