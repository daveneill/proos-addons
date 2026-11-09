"""
ProOS Core — real photo search for backgrounds (areas, the Home page, scenes). Register 1281.

Dave, 10 Nov 2026 08:33: the picture "needs to be a search for anything not just a home … high quality images … if you
typed Aston Martin then it will give you high quality images of an aston martin"; 08:47 "all option added as long as the
UnSplash or Pexels there is no watermarks etc".

THE SOURCE: Pexels' own search API (api.pexels.com/v1/search). Pexels photos carry no watermark and are free to use; the
API asks for a visible "Photos provided by Pexels" link and, where possible, the photographer's name — both are returned
here for the editors to show beside the results. The key is the box's own (saved in Assist's settings beside the image
key, never sent to a page). Nothing is stored: a picked photo is used by its Pexels address, like the curated set.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.pexels.com/v1/search"
_TIMEOUT = 12


def search(query: str, key: str, per_page: int = 15, opener=None) -> dict:
    """{"photos": [{thumb, url, credit, credit_url, link, alt}], "attribution": …} or {"error": plain words}."""
    q = (query or "").strip()
    if not q:
        return {"error": "Type what you are looking for first."}
    if not (key or "").strip():
        return {"error": "Photo search needs a Pexels key — add it in Pro › Tech › Assist.", "needs_key": True}
    url = API + "?" + urllib.parse.urlencode({"query": q, "per_page": max(1, min(int(per_page or 15), 40)),
                                             "orientation": "landscape"})
    req = urllib.request.Request(url, headers={"Authorization": key.strip(), "User-Agent": "ProOS"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"error": "Pexels refused the key — check it in Pro › Tech › Assist.", "needs_key": True}
        if e.code == 429:
            return {"error": "Pexels says too many searches this hour — try again shortly."}
        return {"error": "Pexels didn't answer (%s)." % e.code}
    except Exception:
        return {"error": "Couldn't reach Pexels — check the box's internet."}
    out = []
    for p in data.get("photos") or []:
        src = p.get("src") or {}
        big = src.get("large2x") or src.get("original") or src.get("large")
        if not big:
            continue
        out.append({"thumb": src.get("medium") or src.get("small") or big, "url": big,
                    "credit": p.get("photographer") or "", "credit_url": p.get("photographer_url") or "",
                    "link": p.get("url") or "", "alt": p.get("alt") or ""})
    return {"photos": out, "attribution": {"text": "Photos provided by Pexels", "url": "https://www.pexels.com"}}
