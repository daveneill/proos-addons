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
            out.append({"day": key, "kind": kind, "sessions": row.get("sessions", 0),
                        "minutes": round(float(row.get("seconds") or 0) / 60.0, 1),
                        "responses": row.get("responses", 0),
                        "tokens": {k: row.get(k, 0) for k in FIELDS},
                        "cost_usd": cost(row, prices)})
    return {"days": out, "prices_usd_per_million": dict(PRICES, **(prices or {})),
            "note": "An estimate from published prices — the provider's billing page is the truth."}


def clear() -> None:
    try:
        os.remove(_PATH)
    except FileNotFoundError:
        pass
