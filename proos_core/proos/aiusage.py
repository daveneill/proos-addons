"""WHAT THE REALTIME VOICE COSTS, DAY BY DAY — register 1151 (1 Nov 2026).

Dave, 17:13 "yes both" (to: the phone app, and a usage log first so the real cost on his box is known before it is on
every phone). The provider reports the tokens of every response it finishes (response.done → usage: audio and text, in
and out, cached); the page hands each one to Core, Core keeps a small ledger per day. The cost is an ESTIMATE from the
price table below — the provider's published per-token prices, US dollars, editable in assist.json
("realtime_prices") the day they change. The provider's own billing page is the truth; this says so on the page.
"""
from __future__ import annotations

import datetime
import json
import os
import threading

_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "ai_usage.json")
_LOCK = threading.Lock()
_KEEP_DAYS = 90                      # plumbing: how many days the ledger keeps

# US$ per 1,000,000 tokens. Audio: gpt-realtime-2 as published May 2026 ($32 in / $64 out / $0.40 cached in);
# text: the gpt-realtime text rates ($4 in / $16 out). An estimate, stated as one wherever it is shown.
PRICES = {"audio_in": 32.0, "audio_out": 64.0, "text_in": 4.0, "text_out": 16.0, "cached_in": 0.40}
FIELDS = ("audio_in", "audio_out", "text_in", "text_out", "cached_in")

# REGISTER 1162 — WHAT TYPED AND ROOM-MIC ASSIST COSTS. Dave 2 Nov 06:28: "it has incresed the ChatGPT costs a lot … I am
# also trying to to work out costs per home for possible subscription costs"; 06:38 "yes please" to a ledger. Only Talk
# Live was counted (1151). Now every Assist turn that reaches the model (typed in the app, a room mic, the Health engine's
# own turns) adds its rounds' tokens to the same day ledger, one line per model. US$ per 1,000,000 tokens — input,
# cached input, output — as published (GPT-6.1 Sol from OpenAI's model page; Astra and Luna from price trackers, Nov
# 2026); editable in assist.json "model_prices". A model not in this table is COUNTED but NOT PRICED — the page says
# "price not known", never a guess. Web searches are counted; their per-search charge is priced only when set
# ("web_search_per_call" in model_prices).
MODEL_PRICES = {
    "gpt-6.1-sol": {"text_in": 2.0, "cached_in": 0.10, "text_out": 10.0},
    "gpt-6-sol": {"text_in": 2.0, "cached_in": 0.20, "text_out": 10.0},
    "gpt-6-astra": {"text_in": 10.0, "cached_in": 1.0, "text_out": 50.0},
    "gpt-6-luna": {"text_in": 0.10, "cached_in": 0.01, "text_out": 0.50},
    # REGISTER 1334: the Claude models, counted from 13 Nov. US$ per 1M — input, cache read, output — from a published
    # summary of Anthropic's price list (cloudinsight.cc, checked 30 Sep / 28 Oct 2026); Haiku 5.5 at its up-to-100K tier.
    # Anthropic's billing page is the truth; editable in assist.json "model_prices".
    "claude-opus-5": {"text_in": 5.0, "cached_in": 0.50, "text_out": 25.0},
    "claude-opus-5-5": {"text_in": 4.0, "cached_in": 0.20, "text_out": 20.0},
    "claude-sonnet-5-5": {"text_in": 2.0, "cached_in": 0.10, "text_out": 10.0},
    "claude-haiku-5-5": {"text_in": 0.10, "cached_in": 0.01, "text_out": 0.50},
}


def _load() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, _PATH)
    except Exception:                                            # noqa: BLE001
        pass


def tokens_from(usage: dict) -> dict:
    """The provider's response.done usage → our five counts (cached tokens are counted once, at the cached price)."""
    u = usage or {}
    i = u.get("input_token_details") or {}
    o = u.get("output_token_details") or {}
    cached = int(i.get("cached_tokens") or 0)
    ci = i.get("cached_tokens_details") or {}
    c_audio, c_text = int(ci.get("audio_tokens") or 0), int(ci.get("text_tokens") or 0)
    if cached and not (c_audio or c_text):
        c_text = cached                                          # no split given: count it against text
    return {"audio_in": max(0, int(i.get("audio_tokens") or 0) - c_audio),
            "text_in": max(0, int(i.get("text_tokens") or 0) - c_text),
            "cached_in": cached,
            "audio_out": int(o.get("audio_tokens") or 0),
            "text_out": int(o.get("text_tokens") or 0)}


def cost(counts: dict, prices: dict = None) -> float:
    p = dict(PRICES, **(prices or {}))
    return round(sum(float(counts.get(k) or 0) * float(p.get(k) or 0) / 1e6 for k in FIELDS), 4)


def _day(now=None) -> str:
    return (now or datetime.datetime.now()).strftime("%Y-%m-%d")


def record(kind: str, usage: dict = None, seconds: float = 0.0, session: bool = False, now=None) -> dict:
    """Add one response's tokens (and/or a session's seconds) to today's line."""
    counts = tokens_from(usage) if usage else {}
    with _LOCK:
        d = _load()
        day = d.setdefault(_day(now), {})
        row = day.setdefault(kind or "realtime", {"sessions": 0, "seconds": 0.0, "responses": 0, **{k: 0 for k in FIELDS}})
        if session:
            row["sessions"] = int(row.get("sessions") or 0) + 1
        if seconds:
            row["seconds"] = round(float(row.get("seconds") or 0) + float(seconds), 1)
        if usage:
            row["responses"] = int(row.get("responses") or 0) + 1
            for k in FIELDS:
                row[k] = int(row.get(k) or 0) + int(counts.get(k) or 0)
        for old in sorted(d)[:-_KEEP_DAYS]:
            d.pop(old, None)
        _save(d)
        return dict(row)


def summary(prices: dict = None, days: int = 7, now=None) -> dict:
    """The last `days` days, newest first: sessions, minutes, responses, tokens and the estimated cost."""
    d = _load()
    end = now or datetime.datetime.now()
    out = []
    for i in range(days):
        key = (end - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        for kind, row in sorted((d.get(key) or {}).items()):
            line = {"day": key, "kind": kind, "sessions": row.get("sessions", 0),
                    "minutes": round(float(row.get("seconds") or 0) / 60.0, 1),
                    "responses": row.get("responses", 0),
                    "tokens": {k: row.get(k, 0) for k in FIELDS}}
            if ":" in kind:                                      # register 1162: an Assist model's line
                fam, model = kind.split(":", 1)
                mp = model_prices(model, (prices or {}).get("models") if isinstance(prices, dict) else None)
                line.update({"family": fam, "model": model, "turns": row.get("turns", 0), "web": row.get("web", 0)})
                if mp is None:
                    line["cost_usd"] = None
                    line["price"] = "price not known for %s" % model
                else:
                    c = sum(float(row.get(k) or 0) * float(mp.get(k) or 0) / 1e6 for k in ("text_in", "cached_in", "text_out"))
                    per = (prices or {}).get("web_search_per_call") if isinstance(prices, dict) else None
                    if isinstance(per, (int, float)):
                        c += float(per) * int(row.get("web") or 0)
                    line["cost_usd"] = round(c, 4)
            else:
                line["family"] = "realtime"
                line["cost_usd"] = cost(row, prices)
            out.append(line)
    priced = [x for x in out if isinstance(x.get("cost_usd"), (int, float))]
    total = round(sum(x["cost_usd"] for x in priced), 4)
    span = len({x["day"] for x in out}) or 1
    return {"days": out, "prices_usd_per_million": dict(PRICES, **(prices or {})),
            "total_usd": total, "days_with_use": span,
            "month_usd": round(total / days * 30, 2) if out else 0.0,
            "unpriced": sorted({x.get("model") for x in out if x.get("cost_usd") is None}),
            "note": "An estimate from published prices — the provider's billing page is the truth."}


def model_prices(model: str, override: dict = None):
    """The per-million prices for a chat model, or None when it is not known (never guessed)."""
    table = dict(MODEL_PRICES)
    for k, v in ((override or {}).items() if isinstance(override, dict) else []):
        if isinstance(v, dict):
            table[str(k)] = v
    return table.get(str(model or "").strip().lower())


def record_turn(kind: str, model: str, steps, now=None) -> dict:
    """One Assist turn's model rounds (Core's own per-round record: input, cached, output tokens; web searches) added to
    today's line for "<kind>:<model>"."""
    steps = [s for s in (steps or []) if isinstance(s, dict)]
    if not steps:
        return {}
    key = "%s:%s" % (kind or "assist", str(model or "?").strip().lower())
    with _LOCK:
        d = _load()
        day = d.setdefault(_day(now), {})
        row = day.setdefault(key, {"sessions": 0, "seconds": 0.0, "responses": 0, "turns": 0, "web": 0,
                                   **{k: 0 for k in FIELDS}})
        row["turns"] = int(row.get("turns") or 0) + 1
        for st in steps:
            inp, cached = int(st.get("input") or 0), int(st.get("cached") or 0)
            row["responses"] = int(row.get("responses") or 0) + 1
            row["text_in"] = int(row.get("text_in") or 0) + max(0, inp - cached)
            row["cached_in"] = int(row.get("cached_in") or 0) + cached
            row["text_out"] = int(row.get("text_out") or 0) + int(st.get("output") or 0)
            row["web"] = int(row.get("web") or 0) + int(st.get("web") or 0)
        for old in sorted(d)[:-_KEEP_DAYS]:
            d.pop(old, None)
        _save(d)
        return dict(row)


def clear() -> None:
    try:
        os.remove(_PATH)
    except FileNotFoundError:
        pass
