"""ProOS Core — the site area, known by its label, never by its name.

REGISTER 440 (13 Sep 2026). The whole-home "Home" area — the standing
infrastructure room whose picture is the Dashboard's Home background — was
found in five places by the literal name "Home". Rename it and a second
"Home" appears on the next boot (Dave, 4 Aug: "not sure why I have 2
Homes"). ProOS already stamps that area with the platform's own
`dashboard_system` label at provisioning; the label is id-based and
survives any rename. This is the ONE place the question "is this the site
area?" is answered, and the answer never reads the name.
"""
from __future__ import annotations

LABEL = "dashboard_system"


def is_site(area: dict | None) -> bool:
    """True when the area carries the site label."""
    try:
        return LABEL in ((area or {}).get("labels") or [])
    except Exception:  # noqa: BLE001
        return False


def find(areas) -> dict | None:
    """The site area among the platform's areas, or None. The first labelled
    area wins in the platform's own order; there should only ever be one."""
    for a in (areas or []):
        if is_site(a):
            return a
    return None


def find_id(areas) -> str | None:
    a = find(areas)
    return (a or {}).get("area_id") or None
