"""
ProOS's own state ABOUT Music Assistant — and the one place that forgets it.

Dave, 13 Aug 2026, having made ProOS Music an OPTIONAL integration: "I just
want to confirm that when we add or remove it, it is really removing
everything." It was not. Removing MA left the HOUSE clean — every twin entity
gone, every speaker's source list clean — while ProOS kept four stores about a
server that no longer existed, with NO unlink path anywhere in the codebase.
They cleared only on a factory reset (register 134).

The dangerous one is the room-speaker allowlist: player_ids minted by the OLD
MA instance. Re-add MA, the new instance mints different ids, and the stale
list filters the wrong set — speakers hidden, or nothing shown, silently and
with no error. A thing installers ADD AND REMOVE must add and remove cleanly,
or the second install behaves unlike the first, and that is discovered at a
client's house.

ONE MECHANISM: these paths live here and nowhere else, so a new store cannot
be added without appearing in STORES — which the residue bench pins.
"""
import json
import os

DATA_DIR = os.environ.get("PROOS_DATA_DIR", "/data")

# Every store ProOS keeps ABOUT Music Assistant. The bench asserts this list
# is complete; adding a store without adding it here reds the gate.
STORES = ("ma_conn.json", "ma_admin.json",
          "music_speakers.json", "music_genres.json")


def path(name):
    """The one place a ProOS-about-MA store's path is decided."""
    return os.path.join(DATA_DIR, name)


def _path(name):
    return path(name)


def _read(name):
    try:
        with open(_path(name)) as f:
            return json.load(f)
    except Exception:                                            # noqa: BLE001
        return None


def _write(name, payload):
    with open(_path(name), "w") as f:
        json.dump(payload, f)


# ── connection ─────────────────────────────────────────────────────────────
def load_conn():
    """(host, port, token) or None."""
    d = _read("ma_conn.json") or {}
    h, p = d.get("host"), d.get("port")
    if h and p:
        return str(h), int(p), d.get("token")
    return None


def save_conn(host, port, token):
    _write("ma_conn.json", {"host": str(host), "port": int(port),
                            "token": token})


# ── admin identity ─────────────────────────────────────────────────────────
def load_admin_token():
    return (_read("ma_admin.json") or {}).get("token")


def save_admin_token(token):
    d = _read("ma_admin.json") or {}
    d["token"] = token
    _write("ma_admin.json", d)


def load_admin_user():
    d = _read("ma_admin.json") or {}
    u = d.get("user")
    return u if isinstance(u, dict) and u.get("id") else None


def save_admin_user(uid, username, display_name):
    d = _read("ma_admin.json") or {}
    d["user"] = {"id": uid, "username": username, "name": display_name}
    _write("ma_admin.json", d)


def clear_admin_token():
    d = _read("ma_admin.json") or {}
    d.pop("token", None)
    _write("ma_admin.json", d)


# ── curated room speakers ──────────────────────────────────────────────────
def load_speakers():
    """The kept player_ids, or None when this home has never curated."""
    ids = (_read("music_speakers.json") or {}).get("player_ids")
    return [str(x) for x in ids] if isinstance(ids, list) else None


def save_speakers(ids):
    out = sorted({str(x) for x in (ids or [])})
    _write("music_speakers.json", {"player_ids": out})
    return out


def validate_speakers(live_player_ids):
    """Drop curated ids the LIVE engine does not know, and persist the repair.

    Returns (kept, dropped). The stale-allowlist fault of register 134 heals
    itself here — once, at the moment the truth is available.

    UNKNOWN IS NOT ZERO (registers 105, 108). If the engine cannot be asked
    (None) or answers with nothing (a server still starting), the stored list
    is returned UNTOUCHED. Wiping a curated list because a server was briefly
    down would be the same fault this codebase keeps chasing: acting on an
    absence nobody verified.
    """
    stored = load_speakers()
    if stored is None:
        return None, []                      # never curated stays uncurated
    if not live_player_ids:
        return stored, []                    # unknown -> change nothing
    live = {str(x) for x in live_player_ids}
    keep = [i for i in stored if i in live]
    dropped = [i for i in stored if i not in live]
    if dropped:
        save_speakers(keep)                  # heal once, not on every read
    return keep, dropped


# ── curated genres ─────────────────────────────────────────────────────────
def load_genres():
    g = (_read("music_genres.json") or {}).get("genres")
    return [str(x) for x in g] if isinstance(g, list) else None


def save_genres(names):
    out = sorted({str(x) for x in (names or [])})
    _write("music_genres.json", {"genres": out})
    return out


# ── the unlink ─────────────────────────────────────────────────────────────
def present():
    """Which of ProOS's MA stores exist right now."""
    return [n for n in STORES if os.path.exists(_path(n))]


def forget():
    """Remove every store ProOS keeps about MA. Returns what was cleared.

    Called when the installer turns ProOS Music OFF. Safe to call on a box
    that never had it — an already-clean home returns []."""
    cleared = []
    for name in STORES:
        p = _path(name)
        try:
            if os.path.exists(p):
                os.remove(p)
                cleared.append(name)
        except OSError:
            pass
    return cleared
