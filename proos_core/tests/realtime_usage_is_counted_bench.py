"""WHAT THE REALTIME VOICE COSTS IS COUNTED — register 1151 (1 Nov 2026).
Run: python3 tests/realtime_usage_is_counted_bench.py

Dave 17:13 "yes both" — a usage log before the realtime voice goes on every phone, so its real cost on his box is known.
DRIVEN: the real proos/aiusage.py with responses shaped like the provider's response.done usage.
  1. a response's audio/text in/out and cached tokens are counted once each (cached at the cached price)
  2. the day's line adds sessions, seconds and responses; seven days come back newest first with minutes and cost
  3. the cost is the published per-token prices (editable) — a Ryan-mode answer (text only out) costs far less than the
     same answer spoken by the provider (audio out)
  4. factory reset clears the ledger; Core's routes are owner/tech; Pro posts every response.done's usage and shows it
"""
import datetime
import os
import sys
import tempfile

os.environ["PROOS_DATA_DIR"] = tempfile.mkdtemp(prefix="proos_aiu_")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import aiusage as U  # noqa: E402

passed = failed = 0


def ok(c, m):
    global passed, failed
    if c:
        passed += 1
        print("  ok  " + m)
    else:
        failed += 1
        print("  FAIL " + m)


spoken = {"input_token_details": {"audio_tokens": 400, "text_tokens": 9000, "cached_tokens": 8000,
                                  "cached_tokens_details": {"text_tokens": 8000, "audio_tokens": 0}},
          "output_token_details": {"audio_tokens": 300, "text_tokens": 60}}
ryan = {"input_token_details": {"audio_tokens": 400, "text_tokens": 9000, "cached_tokens": 8000,
                                "cached_tokens_details": {"text_tokens": 8000, "audio_tokens": 0}},
        "output_token_details": {"audio_tokens": 0, "text_tokens": 60}}
t = U.tokens_from(spoken)
ok(t == {"audio_in": 400, "text_in": 1000, "cached_in": 8000, "audio_out": 300, "text_out": 60},
   "1 · tokens counted once each, cached at the cached price (%s)" % t)
c_sp, c_ry = U.cost(U.tokens_from(spoken)), U.cost(U.tokens_from(ryan))
ok(c_ry < c_sp and c_sp == round((400*32 + 1000*4 + 8000*0.4 + 300*64 + 60*16) / 1e6, 4),
   "3 · cost from the published prices; Ryan mode (no audio out) cheaper: US$%.4f vs US$%.4f" % (c_ry, c_sp))
now = datetime.datetime(2026, 11, 1, 17, 0)
U.record("realtime", session=True, now=now)
U.record("realtime", usage=spoken, now=now)
U.record("realtime", usage=ryan, now=now)
U.record("realtime", seconds=90, now=now)
U.record("realtime", session=True, seconds=30, now=now - datetime.timedelta(days=1))
s = U.summary(now=now)
d0, d1 = s["days"][0], s["days"][1]
ok(d0["day"] == "2026-11-01" and d0["sessions"] == 1 and d0["minutes"] == 1.5 and d0["responses"] == 2
   and abs(d0["cost_usd"] - round(c_sp + c_ry, 4)) < 1e-4 and d1["day"] == "2026-10-31" and d1["minutes"] == 0.5,
   "2 · the day's line and seven days newest first (%s · %s min · US$%s)" % (d0["day"], d0["minutes"], d0["cost_usd"]))
ok("estimate" in s["note"].lower() and s["prices_usd_per_million"]["audio_out"] == 64.0, "  …said as an estimate, with the prices used")
ok(U.summary(prices={"audio_out": 1.0}, now=now)["days"][0]["cost_usd"] < d0["cost_usd"], "  …and the prices are editable")
U.clear()
ok(U.summary(now=now)["days"] == [], "4 · cleared → empty")
srv = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
pro = open(os.path.join(HERE, "..", "..", "deploy", "pro.html"), encoding="utf-8").read()
ok("_aiusage_mod.clear()" in srv and srv.count('parts == ["assist", "realtime", "usage"]') == 2,
   "  …factory reset clears it; Core reads and writes it on its own route")
# RE-PINNED, register 1348: Pro's Realtime prototype (which posted its usage) is gone; the app's Talk Live posts every
# finished response's usage, and what Pro shows is ProOS Connect's own count for the month (Connect adds the key).
dash = open(os.path.join(HERE, "..", "..", "deploy", "dashboard.html"), encoding="utf-8").read()
ok("lvUsage({usage:ev.response.usage})" in dash and "/assist/realtime/usage" in dash and "This Month · <b>$" in pro,
   "  …the app posts every finished response's usage; Pro shows the month as ProOS Connect counts it")

# REGISTER 1162 — typed and room-mic Assist counted, per model. The steps are Core's own per-round record, measured on
# the box 2 Nov (the lamps question: 12,364 in / 0 cached / 36 out, then 12,588 in / 12,361 cached / 29 out).
STEPS = [{"input": 12364, "cached": 0, "output": 36, "web": 0}, {"input": 12588, "cached": 12361, "output": 29, "web": 0}]
U.record_turn("assist", "gpt-6.1-sol", STEPS, now=now)
U.record_turn("assist", "GPT-6-Astra", STEPS, now=now)
U.record_turn("engine", "some-new-model", STEPS[:1], now=now)
sm = U.summary(now=now)
by = {r["kind"]: r for r in sm["days"]}
sol, ast_, unk = by.get("assist:gpt-6.1-sol"), by.get("assist:gpt-6-astra"), by.get("engine:some-new-model")
want_sol = round((12364 + 227) * 2.0 / 1e6 + 12361 * 0.10 / 1e6 + 65 * 10.0 / 1e6, 4)
want_ast = round((12364 + 227) * 10.0 / 1e6 + 12361 * 1.0 / 1e6 + 65 * 50.0 / 1e6, 4)
ok(sol and sol["turns"] == 1 and sol["responses"] == 2 and sol["tokens"]["text_in"] == 12591 and sol["tokens"]["cached_in"] == 12361
   and abs(sol["cost_usd"] - want_sol) < 1e-4 and ast_ and abs(ast_["cost_usd"] - want_ast) < 1e-4,
   "5 · an Assist turn is counted per model — Sol US$%s, Astra US$%s for the same question (1162)" % (sol and sol["cost_usd"], ast_ and ast_["cost_usd"]))
ok(unk and unk["cost_usd"] is None and "price not known" in unk["price"] and "some-new-model" in sm["unpriced"]
   and unk["family"] == "engine", "  …a model with no published price is COUNTED, NOT PRICED — never guessed; the Health engine on its own line")
ok(abs(sm["total_usd"] - round(want_sol + want_ast, 4)) < 1e-4 and sm["month_usd"] == round(sm["total_usd"] / 7 * 30, 2),
   "  …the seven days' total and what that rate comes to over a month")
ok(U.summary(prices={"models": {"some-new-model": {"text_in": 1.0, "cached_in": 0.1, "text_out": 2.0}}}, now=now)["unpriced"] == [],
   "  …a price added in assist.json (model_prices) prices it")
A = open(os.path.join(HERE, "..", "proos", "assist.py"), encoding="utf-8").read()
seg = A[A.index("def _chat_openai("):A.index("def _count_turn(")]
ok(seg.count('_count_turn(runner, model, _tm["steps"])') == 2 and "aiusage.record_turn(kind, model, steps)" in A
   and "/assist/connect" in pro and "(m.calls||0)" in pro,
   "6 · WIRING: both ends of an OpenAI turn are counted; Pro shows this home's AI use from ProOS Connect (1348)")

print("\n%d checks passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
