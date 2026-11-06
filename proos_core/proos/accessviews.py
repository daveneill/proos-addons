"""UNIFI ACCESS — register 1241 (Dave, 5 Nov: Access "I will setup later but still needs to be built"; 6 Nov: "finish all
the build please").

Built on the OFFICIAL UniFi Access developer interface (the Access API reference; port 12445; the Access app's own token),
before Access is on Dave's console — so NOTHING here is measured on his box yet; each view takes only the fields the
reference documents and passes nothing else. People carry PINs, NFC card tokens, face and touch-pass data — none of that
leaves Core.
"""


def _s(v, n=80):
    return str(v)[:n] if isinstance(v, (str, int, float)) and str(v).strip() else None


def doors_view(doors):
    out = []
    for d in (doors or []):
        if not isinstance(d, dict):
            continue
        lk, pos = str(d.get("door_lock_relay_status") or "").lower(), str(d.get("door_position_status") or "").lower()
        out.append({"id": _s(d.get("id"), 64), "name": _s(d.get("full_name") or d.get("name")) or "Door",
                    "locked": True if lk == "lock" else False if lk == "unlock" else None,
                    "open": True if pos == "open" else False if pos == "close" else None,
                    "has_hub": bool(d.get("is_bind_hub"))})
    out.sort(key=lambda x: str(x["name"]).lower())
    return out


def people_view(users):
    out = []
    for u in (users or []):
        if not isinstance(u, dict):
            continue
        name = _s(u.get("full_name")) or " ".join(x for x in (_s(u.get("first_name")), _s(u.get("last_name"))) if x) or "Person"
        out.append({"name": name, "status": _s(u.get("status"), 20), "number": _s(u.get("employee_number"), 30),
                    "has_pin": bool(u.get("pin_code")), "cards": len(u.get("nfc_cards") or []) if isinstance(u.get("nfc_cards"), list) else 0})
    out.sort(key=lambda x: x["name"].lower())
    return out


def visitors_view(visitors):
    out = []
    for v in (visitors or []):
        if not isinstance(v, dict):
            continue
        name = " ".join(x for x in (_s(v.get("first_name")), _s(v.get("last_name"))) if x) or "Visitor"
        out.append({"name": name, "status": _s(v.get("status"), 20), "reason": _s(v.get("visit_reason"), 60),
                    "from": v.get("start_time") if isinstance(v.get("start_time"), (int, float)) else None,
                    "until": v.get("end_time") if isinstance(v.get("end_time"), (int, float)) else None})
    return out


def activity_view(logs):
    """The Access log: who, what, which door, the result, when — names only."""
    hits = (logs or {}).get("hits") if isinstance(logs, dict) else logs
    out = []
    for h in (hits or []):
        src = (h or {}).get("_source") if isinstance(h, dict) else None
        if not isinstance(src, dict):
            continue
        actor = src.get("actor") if isinstance(src.get("actor"), dict) else {}
        ev = src.get("event") if isinstance(src.get("event"), dict) else {}
        tg = [t.get("display_name") for t in (src.get("target") or []) if isinstance(t, dict) and t.get("type") in ("door", "device") and _s(t.get("display_name"))]
        out.append({"who": _s(actor.get("display_name")) or "Someone", "what": _s(ev.get("display_message") or ev.get("type"), 80),
                    "result": _s(ev.get("result"), 20), "where": _s(tg[0]) if tg else None,
                    "when": ev.get("published") if isinstance(ev.get("published"), (int, float)) else None})
    out.sort(key=lambda x: x["when"] or 0, reverse=True)
    return out[:200]


LOCK_RULES = ("keep_unlock", "keep_lock", "reset")


def emergency_view(e):
    e = e if isinstance(e, dict) else {}
    return {"lockdown": bool(e.get("lockdown")), "evacuation": bool(e.get("evacuation"))}
