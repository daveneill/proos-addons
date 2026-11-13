"""CLAUDE TURNS ARE COUNTED — register 1334 (13 Nov 2026).
Run: python3 tests/claude_turns_are_counted_bench.py

Read on Dave's box 13 Nov: the cost ledger's last line was 10 Nov and every line was OpenAI's — only the OpenAI road
called _count_turn, so from the day Claude became the brain Pro showed nothing spent. DRIVEN — the real _chat_claude with
the provider stood in, the real aiusage ledger in a scratch folder:
  1. a two-round Claude turn adds ONE turn and both rounds' tokens to today's "assist:<model>" line
  2. input counts fresh + cache-read + cache-written; cached counts cache-read; output; the provider's web searches
  3. a turn that ends any way (an answer, out of room mid-call) is counted once, never twice
  4. the Claude models are priced; a model not in the table is counted and shown unpriced, never guessed
"""
import os
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_cost_")

from proos import assist  # noqa: E402
from proos import aiusage  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  %s  wanted %r got %r" % (name, want, got))


class Runner:
    user = {"id": "dave"}

    def run(self, name, args):
        return {"state": "on"}


def drive(rounds, model="claude-opus-5-5"):
    seq = list(rounds)

    def provider(url, payload, headers, on_text=None):
        return seq.pop(0)

    real = (assist._http_stream_claude, assist._claude_tools, assist._claude_system)
    assist._http_stream_claude = provider
    assist._claude_tools = lambda r: []
    assist._claude_system = lambda d, c: "sys"
    try:
        return assist._chat_claude({"model": model, "api_key": "k"}, "doc", "ctx",
                                   [{"role": "user", "content": "is the office light on"}], Runner())
    finally:
        assist._http_stream_claude, assist._claude_tools, assist._claude_system = real


def today(model="claude-opus-5-5"):
    d = aiusage._load()
    return (d.get(aiusage._day()) or {}).get("assist:" + model) or {}


aiusage.clear()
R1 = {"content": [{"type": "tool_use", "id": "t1", "name": "ha_get_state", "input": {"entity_id": "light.office"}}],
      "stop_reason": "tool_use",
      "usage": {"input_tokens": 1000, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 500,
                "output_tokens": 40, "server_tool_use": {"web_search_requests": 1}}}
R2 = {"content": [{"type": "text", "text": "Yes, the Office light is on."}], "stop_reason": "end_turn",
      "usage": {"input_tokens": 200, "cache_read_input_tokens": 10000, "output_tokens": 12}}
drive([R1, R2])
row = today()
check("1 · one Claude turn of two rounds is ONE turn on today's assist line", (row.get("turns"), row.get("responses")), (1, 2))
check("2 · input = fresh + cache-read + cache-written; cached = cache-read; output; web searches",
      (row.get("text_in"), row.get("cached_in"), row.get("text_out"), row.get("web")), (1700, 19000, 52, 1))
CUT = {"content": [{"type": "text", "text": "x"}, {"type": "tool_use", "id": "t9", "name": "ha_call_service", "input": {}}],
       "stop_reason": "max_tokens", "usage": {"input_tokens": 300, "output_tokens": 8000}}
drive([CUT])
row = today()
check("3 · a turn that ends out of room mid-call is counted too — once", (row.get("turns"), row.get("responses")), (2, 3))
s = aiusage.summary(days=7)
line = next(x for x in s["days"] if x.get("model") == "claude-opus-5-5")
want = round((1700 + 300) * 4.0 / 1e6 + 19000 * 0.20 / 1e6 + (52 + 8000) * 20.0 / 1e6, 4)
check("4 · Claude Opus 5.5 is priced (US$4 in / $0.20 cached / $20 out per million)", line.get("cost_usd"), want)
drive([R2], model="claude-mystery-9")
s = aiusage.summary(days=7)
m = next(x for x in s["days"] if x.get("model") == "claude-mystery-9")
check("   …and a model not in the table is counted but shown unpriced, never guessed",
      (m.get("turns"), m.get("cost_usd"), "claude-mystery-9" in s["unpriced"]), (1, None, True))
print("\n%d passed, %d failed" % (PASS, len(FAIL)))
sys.exit(1 if FAIL else 0)
