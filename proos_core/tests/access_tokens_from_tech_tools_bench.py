"""Access tokens from Pro's Tech Tools — REGISTER 1466 (one-list 10.37).  DRIVEN.
Run: python3 tests/access_tokens_from_tech_tools_bench.py

Dave 25 Nov 2026: the home unit let a token be made on the platform's own profile page, so the sessions had a direct tool link to
the box; the G-Flip hides that page, "and pro cant generate a token like native HA so can this be added to Pro"; "just follow the
tech tools gate". A token is made, listed and revoked with the PLATFORM'S OWN commands on the signed-in person's own sign-in
(auth/long_lived_access_token, auth/refresh_tokens, auth/delete_refresh_token) — the mechanism Core already uses for its own
installer sign-in (proauth). Driven here on a stand-in platform that keeps tokens the way the platform does:
 1. create: the platform is asked once, for one year, under the name typed; the token comes back
 2. the name is needed, kept short, and one name is one token
 3. list: only long-lived tokens, the newest first, the one in use marked
 4. revoke: asked of the platform, then READ BACK — it is only said gone when the platform no longer lists it
 5. the token in use by this sign-in is never revoked from here; an id that is not on this account is refused
 6. a platform that does not answer is said so — nothing is invented
 7. (wording) the two doors in server.py stand behind the Tech Tools gate, and the module rides in the zip
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from proos import accesstokens as AT  # noqa: E402

PASS, FAIL = 0, []


def check(name, got, want=True):
    global PASS
    if got == want:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + "  wanted %r got %r" % (want, got))


class Platform:
    """The platform's token list for one person, and the three commands."""
    def __init__(self):
        self.rows = [
            {"id": "r-normal", "type": "normal", "client_name": None, "created_at": "2026-11-01T00:00:00+00:00", "is_current": False},
            {"id": "r-pro", "type": "long_lived_access_token", "client_name": "ProOS Pro (installer)",
             "created_at": "2026-11-02T00:00:00+00:00", "last_used_at": "2026-11-25T01:00:00+00:00", "is_current": True},
            {"id": "r-old", "type": "long_lived_access_token", "client_name": "Old Tool",
             "created_at": "2026-10-01T00:00:00+00:00", "last_used_at": None, "is_current": False}]
        self.calls, self.keep_on_delete, self.down = [], False, False

    def ws(self, token, cmd, **f):
        self.calls.append((token, cmd, f))
        if self.down:
            raise RuntimeError("no answer")
        if cmd == "auth/refresh_tokens":
            return [dict(r) for r in self.rows]
        if cmd == "auth/long_lived_access_token":
            self.rows.append({"id": "r-new%d" % len(self.rows), "type": "long_lived_access_token",
                              "client_name": f["client_name"], "created_at": "2026-11-25T03:00:00+00:00", "is_current": False})
            return "eyJ.made.token"
        if cmd == "auth/delete_refresh_token":
            if not self.keep_on_delete:
                self.rows = [r for r in self.rows if r["id"] != f["refresh_token_id"]]
            return None
        raise AssertionError(cmd)


P = Platform()
AT._ws = P.ws

r = AT.create("me-token", "  Claude on the Mac  ")
made = [c for c in P.calls if c[1] == "auth/long_lived_access_token"]
check("1 · create asks the platform once, on the caller's own sign-in, for one year, under the name typed",
      [(c[0], c[2]) for c in made], [("me-token", {"client_name": "Claude on the Mac", "lifespan": 365})])
check("1 · …and the token comes back with its name and its life", (r.get("ok"), r.get("token"), r.get("name"), r.get("days")),
      (True, "eyJ.made.token", "Claude on the Mac", 365))

check("2 · no name → refused, the platform not asked", (AT.create("me-token", "   ").get("ok"), len(made)),
      (False, len([c for c in P.calls if c[1] == "auth/long_lived_access_token"])))
check("2 · a name over 60 characters is refused", AT.create("me-token", "x" * 61).get("ok"), False)
r2 = AT.create("me-token", "claude on the mac")
check("2 · one name is one token (whatever the capitals) — said, not made twice",
      (r2.get("ok"), "already" in str(r2.get("error")), len([c for c in P.calls if c[1] == "auth/long_lived_access_token"])),
      (False, True, 1))

L = AT.listing("me-token")
check("3 · the list holds long-lived tokens only, the newest first",
      [t["name"] for t in L["tokens"]], ["Claude on the Mac", "ProOS Pro (installer)", "Old Tool"])
check("3 · …the one this sign-in is using is marked, with when each was made and last used",
      [(t["id"], t["current"], t["last_used"]) for t in L["tokens"] if t["id"] in ("r-pro", "r-old")],
      [("r-pro", True, "2026-11-25T01:00:00+00:00"), ("r-old", False, None)])
check("3 · …and no token's secret is ever in the list", any("token" in t for t in L["tokens"]), False)

rv = AT.revoke("me-token", "r-old")
check("4 · revoke asks the platform for that id, then reads the list back: gone",
      (rv.get("ok"), [c[2] for c in P.calls if c[1] == "auth/delete_refresh_token"], P.calls[-1][1]),
      (True, [{"refresh_token_id": "r-old"}], "auth/refresh_tokens"))
P.keep_on_delete = True
new_id = [t["id"] for t in AT.listing("me-token")["tokens"] if t["name"] == "Claude on the Mac"][0]
rv = AT.revoke("me-token", new_id)
check("4 · the platform says nothing but still lists it → NOT said gone", (rv.get("ok"), "still" in str(rv.get("error"))), (False, True))
P.keep_on_delete = False

n = len([c for c in P.calls if c[1] == "auth/delete_refresh_token"])
rv = AT.revoke("me-token", "r-pro")
check("5 · the token this sign-in is using is never revoked here — the platform is not asked",
      (rv.get("ok"), len([c for c in P.calls if c[1] == "auth/delete_refresh_token"])), (False, n))
check("5 · an id that is not on this account is refused", AT.revoke("me-token", "r-nobody").get("ok"), False)
check("5 · a sign-in token is not a tool token: it cannot be revoked from here", AT.revoke("me-token", "r-normal").get("ok"), False)

P.down = True
check("6 · the platform does not answer: list says so", (AT.listing("me-token").get("ok"), "error" in AT.listing("me-token")), (False, True))
check("6 · …create says so and returns no token", (AT.create("me-token", "Another").get("ok"), "token" in AT.create("me-token", "Another")), (False, False))
check("6 · …and no platform's or supplier's name is in what is said",
      any(w in str(AT.listing("me-token").get("error")).lower() for w in ("home assistant", "hass")), False)
P.down = False

SRV = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()
ZIP = open(os.path.join(HERE, "..", "zipfiles.txt"), encoding="utf-8").read().split("\n")
gate = 'if not (users and u and (users.is_tech(u.get("id")) or u.get("is_owner"))):\n                    return self._send(403, {"error": "tech access required"})'
gi = SRV.find('if parts == ["tech", "tokens"]:')
pi = SRV.find('if parts[:2] == ["tech", "tokens"]:')
check("7 · (wording) the reading door stands behind the Tech Tools gate", gi > 0 and gate in SRV[gi:gi + 700])
check("7 · (wording) the making and revoking door stands behind the same gate", pi > 0 and gate in SRV[pi:pi + 700])
check("7 · (wording) both use the CALLER'S own sign-in, never Core's", SRV.count("auth.bearer(self.headers)") >= 3
      and "_at.listing(auth.bearer(self.headers))" in SRV and "_at.create(auth.bearer(self.headers)" in SRV)
check("7 · the module rides in the zip", "proos/accesstokens.py" in ZIP)

print("\n%d checks passed, %d failed" % (PASS, len(FAIL)) + ("  " + repr(FAIL) if FAIL else ""))
sys.exit(1 if FAIL else 0)
