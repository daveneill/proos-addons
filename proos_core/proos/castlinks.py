"""
Cast links — which Cast player is the other half of which Android TV box (a Shield), linked by the installer (register 1059).

Dave, 28 Oct 2026 (ruling 1 on the TV & Shield mockup): "yes" — the Shield's Cast side is linked once by the installer in
Pro, never assumed. 22:04 "Go"; the Cast Side line goes on the Shield's row in the room page.

Why a link is needed: the platform holds a Shield as TWO devices — the Android TV Remote integration (keys, the app that is
open, volume; it carries the MAC) and the Google Cast integration (a picture and a title, but only while something is cast;
it carries no MAC). Nothing on the box proves which Cast player is which Shield (register 1047), so ProOS never joins them
by itself. A same maker / model / name / room match is offered as a SUGGESTION, marked as one, and the installer's Link is
the confirmation — who and when are kept, and Unlink undoes it.

Pure store over /data/cast_links.json, keyed by the box's Android TV Remote media player. No platform calls here: the server
reads the registry and passes what it read in; the dashboard reads the Cast player's own state only when a link exists.
"""
import json
import os
import time

SHIELD_PLATFORM = "androidtv_remote"
CAST_PLATFORM = "cast"


def _path() -> str:
    return os.path.join(os.environ.get("PROOS_DATA_DIR", "/data"), "cast_links.json")


def _all() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("links"), dict):
            return d
    except Exception:                                            # noqa: BLE001
        pass
    return {"links": {}}


def _write(d: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, path)


def links() -> dict:
    """{box media player: {"cast": cast media player, "by": who, "at": when}} — installer links only."""
    return {k: dict(v) for k, v in _all()["links"].items() if isinstance(v, dict) and v.get("cast")}


def cast_for(box: str):
    """The Cast player the installer linked to this box, or None. Never a guess."""
    v = links().get(box)
    return v["cast"] if v else None


def link(box: str, cast: str, who: str, boxes, casts) -> dict:
    """Link a Cast player to a box. `boxes` / `casts` are the entity ids the server READ from the registry for each
    integration — a link between anything else is refused, as is a Cast player already linked to another box."""
    box, cast = str(box or ""), str(cast or "")
    if box not in set(boxes or ()):
        return {"error": "That isn’t an Android TV box ProOS can see"}
    if cast not in set(casts or ()):
        return {"error": "That isn’t a Cast player ProOS can see"}
    d = _all()
    for other, v in d["links"].items():
        if other != box and isinstance(v, dict) and v.get("cast") == cast:
            return {"error": "That Cast player is already linked to another box"}
    d["links"][box] = {"cast": cast, "by": str(who or ""), "at": int(time.time())}
    _write(d)
    return {"ok": True, "box": box, "link": d["links"][box]}


def unlink(box: str) -> dict:
    d = _all()
    gone = d["links"].pop(str(box or ""), None)
    if gone is None:
        return {"error": "That box isn’t linked"}
    _write(d)
    return {"ok": True, "box": box, "removed": gone.get("cast")}


def _norm(s) -> str:
    return " ".join(str(s or "").split()).lower()


def devices(entity_registry, device_registry, states) -> dict:
    """Pure. Every Android TV box and every Cast player the platform holds, each with its name and room, the box's link
    (if the installer made one) and a SUGGESTION: the one unlinked Cast player whose device has the same maker, model,
    name and room. Two or more such matches suggest nothing (it would be a guess)."""
    names = {}
    for st in states or []:
        names[st.get("entity_id")] = (st.get("attributes") or {}).get("friendly_name") or st.get("entity_id")
    devs = {d.get("id"): d for d in (device_registry or []) if isinstance(d, dict)}

    def facts(e):
        dv = devs.get(e.get("device_id")) or {}
        return {"maker": _norm(dv.get("manufacturer")), "model": _norm(dv.get("model")),
                "name": _norm(dv.get("name_by_user") or dv.get("name")), "area": e.get("area_id") or dv.get("area_id") or ""}

    boxes, casts = [], []
    for e in entity_registry or []:
        eid = e.get("entity_id") or ""
        if not eid.startswith("media_player.") or e.get("disabled_by"):
            continue
        if e.get("platform") == SHIELD_PLATFORM:
            boxes.append((eid, facts(e)))
        elif e.get("platform") == CAST_PLATFORM:
            casts.append((eid, facts(e)))
    lk = links()
    taken = {v["cast"]: b for b, v in lk.items()}
    out_boxes = []
    for eid, f in boxes:
        sug = None
        if eid not in lk:
            m = [c for c, cf in casts if c not in taken and cf["name"] and cf["maker"]
                 and (cf["maker"], cf["model"], cf["name"], cf["area"]) == (f["maker"], f["model"], f["name"], f["area"])]
            sug = m[0] if len(m) == 1 else None
        row = {"entity_id": eid, "name": names.get(eid, eid), "area": f["area"], "suggested": sug}
        if eid in lk:
            row.update({"cast": lk[eid]["cast"], "by": lk[eid].get("by") or "", "at": lk[eid].get("at") or 0})
        out_boxes.append(row)
    out_casts = [{"entity_id": c, "name": names.get(c, c), "area": cf["area"], "linked_to": taken.get(c)} for c, cf in casts]
    return {"boxes": out_boxes, "cast_players": out_casts}
