"""ProOS Core — the KNOWLEDGE STORE: what this product and this house
have learned, written down where Assist can read it.

DAVE'S RULING (19 Aug 2026): "I basically need THIS chat within Pro …
with the exception of having all this project documentation and
claude.md etc here, that should be the only difference into how assist
works in ProOS."

That was the measured difference. A Developer session holds the
project's own documents; Assist held none, and so every fact the
product had learned had to be crammed into its instructions — 3,216
words of them — where it sat in front of every single question whether
it was relevant or not.

TWO LAYERS, and the difference matters:

  SHIPPED  — proos/knowledge/*.md, written with the build. The
             PRODUCT'S knowledge: how devices present themselves, what
             awareness may claim, how a moment is built and proven.
             Read-only on the box; it changes when a build changes it,
             so every ProOS box in the fleet knows the same things.

  SITE     — knowledge_site.json in /data, per-box, written by an
             installer (or by Assist with permission). THIS HOUSE'S
             knowledge: the quirks, the decisions, what was tried. It
             is per-site, so it is wiped by a factory reset, exactly
             like the commissioning record.

A homeowner reads the shipped layer only: site notes are the
professional's record of the install, in the trade's words, and are
not written for the household.

Nothing in here decides anything. It is a filing cabinet with a
search: it answers "what do we know about this", and the reader — the
installer, or the model — decides what to do with the answer. Plain
term matching, no ranking cleverness, stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import time

_HERE = os.path.dirname(__file__)
_SHIPPED_DIR = os.path.join(_HERE, "knowledge")
_SITE_PATH = os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"),
                          "knowledge_site.json")
_SNIPPET = 280
_MAX_BODY = 20000          # a note longer than this is a document, not a note


def _title_of(body: str, fallback: str) -> str:
    """The first markdown heading, or the filename."""
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip() or fallback
        if s:
            break
    return fallback


def shipped() -> list:
    """The product's own knowledge, as it shipped with this build."""
    out = []
    try:
        names = sorted(os.listdir(_SHIPPED_DIR))
    except Exception:                                            # noqa: BLE001
        return out
    for n in names:
        if not n.endswith(".md"):
            continue
        try:
            with open(os.path.join(_SHIPPED_DIR, n), encoding="utf-8") as fh:
                body = fh.read()
        except Exception:                                        # noqa: BLE001
            continue
        out.append({"path": n[:-3], "title": _title_of(body, n[:-3]),
                    "body": body, "layer": "product"})
    return out


def _site_load() -> dict:
    try:
        with open(_SITE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


def _site_save(d: dict) -> None:
    tmp = _SITE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, _SITE_PATH)


def site() -> list:
    """This house's own notes, newest first."""
    recs = list((_site_load() or {}).values())
    recs.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return [dict(r, layer="site") for r in recs]


def docs(pro: bool) -> list:
    """Everything the caller may read. A homeowner gets the product's
    knowledge; the site's own notes are the professional's record."""
    return shipped() + (site() if pro else [])


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")
    return s[:60] or "note"


def _terms(q: str) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", (q or "").lower()) if len(t) > 2]


def search(query: str, pro: bool, limit: int = 5) -> list:
    """Which documents mention these words, and where. The count is the
    whole ranking — no cleverness, and the snippet is the document's own
    words so the reader judges, not this function."""
    terms = _terms(query)
    hits = []
    for d in docs(pro):
        low = (d["body"] or "").lower()
        title_low = (d.get("title") or "").lower()
        score = 0
        first = None
        for t in terms:
            n = low.count(t)
            if n:
                score += n
                i = low.find(t)
                if first is None or i < first:
                    first = i
            if t in title_low:
                score += 5
        if not score:
            continue
        if first is None:
            first = 0
        start = max(0, first - 80)
        hits.append({"path": d["path"], "title": d.get("title"),
                     "layer": d.get("layer"), "score": score,
                     "snippet": (d["body"] or "")[start:start + _SNIPPET].strip()})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:max(1, int(limit or 5))]


def read(path: str, pro: bool):
    """One document, whole. None when the caller may not read it or it
    does not exist — the caller says which, never this function."""
    p = (path or "").strip()
    for d in docs(pro):
        if d["path"] == p:
            return d
    return None


def write(title: str, body: str, author: str = "", path: str = "") -> dict:
    """Add or replace a SITE note. The product's own knowledge ships with
    the build and is never written here."""
    body = (body or "").strip()
    if not body:
        raise ValueError("a note needs a body")
    if len(body) > _MAX_BODY:
        raise ValueError("that is a document, not a note (%d characters, "
                         "limit %d)" % (len(body), _MAX_BODY))
    key = _slug(path or title or body[:40])
    if key in {d["path"] for d in shipped()}:
        raise ValueError("that name belongs to the product's own knowledge "
                         "— choose another")
    d = _site_load()
    prev = d.get(key) or {}
    rec = {"path": key,
           "title": (title or _title_of(body, key)).strip()[:120],
           "body": body,
           "author": author or prev.get("author") or "",
           "created": prev.get("created") or time.time(),
           "updated": time.time()}
    d[key] = rec
    _site_save(d)
    return dict(rec, layer="site")


def delete(path: str) -> bool:
    d = _site_load()
    if (path or "") in d:
        d.pop(path)
        _site_save(d)
        return True
    return False


def stats() -> dict:
    s = shipped()
    st = site()
    return {"product_docs": len(s), "site_notes": len(st),
            "product_words": sum(len((x["body"] or "").split()) for x in s),
            "site_words": sum(len((x["body"] or "").split()) for x in st)}
