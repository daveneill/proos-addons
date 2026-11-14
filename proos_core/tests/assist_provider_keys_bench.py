"""
One API key per provider — run: python3 tests/assist_provider_keys_bench.py

13 Sep 2026 (register 434). Dave switched the Provider dropdown from ChatGPT
to Claude and pressed Test: "HTTP 401 authentication_error: API key is
invalid" — with a valid model. The file held ONE api_key beside the provider,
so the OpenAI key was sent to Anthropic. Keys now live per provider; api_key
is derived (the active provider's key) so every reader stays as it was.

What this holds: a key saved under one provider is never returned as the
other's; switching providers keeps both keys; clearing one leaves the other;
an older file with a flat api_key is read as that provider's key and written
under keys on the next save; status says which providers hold a key; the
flat field is never written to disk again; the stored keys never appear in
status.
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp()
os.environ["PROOS_DATA_DIR"] = _TMP
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from proos import assist                                          # noqa: E402

assist._CFG_PATH = os.path.join(_TMP, "assist.json")
PASS, FAIL = 0, []


def check(name, cond):
    global PASS
    if cond:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name)


def _disk():
    with open(assist._CFG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ── the fault, replayed ─────────────────────────────────────────────────────
assist.save_config({"provider": "openai", "api_key": "sk-openai-1"})
check("OpenAI key saved for OpenAI", assist.load_config().get("api_key") == "sk-openai-1")
assist.save_config({"provider": "claude"})
c = assist.load_config()
check("switching to Claude does NOT hand it the OpenAI key", c.get("api_key") == "")
check("…and the assistant is honestly OFF until Claude has a key",
      assist.status().get("enabled") is False)
st = assist.status(full=True)
check("status says which providers hold a key",
      st.get("keys_saved") == {"claude": False, "openai": True})
check("status never carries a key value", "sk-openai-1" not in json.dumps(st))

# ── both kept ───────────────────────────────────────────────────────────────
assist.save_config({"api_key": "sk-ant-1"})
check("Claude key saved under Claude", assist.load_config().get("api_key") == "sk-ant-1")
check("the OpenAI key is still there", _disk()["keys"].get("openai") == "sk-openai-1")
assist.save_config({"provider": "openai"})
check("switching back returns the OpenAI key", assist.load_config().get("api_key") == "sk-openai-1")
check("enabled again on the switch back", assist.status().get("enabled") is True)
check("status: both saved", assist.status(full=True).get("keys_saved") == {"claude": True, "openai": True})

# ── clearing one leaves the other ───────────────────────────────────────────
assist.save_config({"api_key": ""})
check("empty string clears the ACTIVE provider's key only",
      assist.load_config().get("api_key") == "" and _disk()["keys"].get("claude") == "sk-ant-1")
check("the flat field is never written to disk", "api_key" not in _disk())

# ── an older file migrates ──────────────────────────────────────────────────
with open(assist._CFG_PATH, "w", encoding="utf-8") as fh:
    json.dump({"provider": "openai", "model": "", "api_key": "sk-old"}, fh)
check("a flat api_key from an older build is read as that provider's key",
      assist.load_config().get("api_key") == "sk-old")
check("…and reported as saved for that provider only",
      assist.status(full=True).get("keys_saved") == {"claude": False, "openai": True})
assist.save_config({"model": "gpt-6-astra"})
d = _disk()
check("the next save writes it under keys and drops the flat field",
      d.get("keys") == {"openai": "sk-old"} and "api_key" not in d)

# ── a key with no provider goes nowhere ────────────────────────────────────
assist.save_config({"provider": "", "api_key": "sk-nowhere"})
check("a key saved with no provider is not kept anywhere", "sk-nowhere" not in json.dumps(_disk()))

# ── the image key is untouched by all this ─────────────────────────────────
assist.save_config({"provider": "openai", "image_key": "sk-img"})
check("the scene image key is its own field still", _disk().get("image_key") == "sk-img")

# ── images keep working when chat moves to Claude (register 435) ───────────
assist.save_config({"provider": "openai", "api_key": "sk-openai-2", "image_key": ""})
assist.save_config({"provider": "claude", "api_key": "sk-ant-2"})
check("chat on Claude, no image key typed → images still use the saved OpenAI key",
      assist._image_key(assist.load_config()) == "sk-openai-2")
check("…and status says images are on", assist.status(full=True).get("has_image_key") is True)
assist.save_config({"image_key": "sk-img-2"})
check("a dedicated image key still wins over the chat key",
      assist._image_key(assist.load_config()) == "sk-img-2")
assist.save_config({"provider": "openai", "api_key": "", "image_key": ""})
check("no OpenAI key anywhere → no image key (honest curated fallback)",
      assist._image_key(assist.load_config()) == "")

# ── every reader still reads api_key (so the change is invisible to them) ──
src = open(os.path.join(os.path.dirname(__file__), "..", "proos", "assist.py"), encoding="utf-8").read()
check("no reader reaches into keys[] directly — api_key is the one door",
      # 5 → 6, register 1347: status() also says whether the box still holds a key of its own (box_keys — Pro offers
      # Remove This Box's Keys while ProOS Connect holds ProOS's). A yes/no for the page, never a key; not a reader of one.
      src.count('["keys"]') + src.count("get(\"keys\")") <= 6)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
